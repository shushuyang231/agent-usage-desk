"use strict";

const $ = (id) => document.getElementById(id);
const TAB = {
  overview: ["用量总览", "模型明细", "模型"],
  models: ["模型用量", "模型明细", "模型"],
  projects: ["项目用量", "项目明细", "项目"],
  sessions: ["会话用量", "会话明细", "会话"],
};
const SOURCES = { codex: "Codex", agy: "Antigravity" };
const root = document.documentElement;
const chartColors = {};
const CHART_TYPE = '"Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", sans-serif';
const COLUMNS = {
  day: 0, source: 1, model: 2, project: 3, session: 4, fresh: 5, cache: 6,
  write: 7, output: 8, reasoning: 9, cost: 10, unpriced: 11, calls: 12, approximate: 13, charges: 14,
};
const { day, source, model, project, session, fresh, cache, write, output, reasoning,
  cost, unpriced, charges } = COLUMNS;
const amount = (row) => row[fresh] + row[cache] + row[write] + row[output];
const numberFormat = new Intl.NumberFormat("en-US");
const formatInteger = (value) => numberFormat.format(value);
const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const detailedUSD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 9 });
const localDateTime = new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short", hour12: false });
const displayUSD = (value) => value > 0 && value < .005 ? "< $0.01" : usd.format(value);
const percent = (fraction) => (fraction * 100).toFixed(1) + "%";
const compact = (n) => {
  for (const [size, unit] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]]) {
    if (n >= size) return (n / size).toFixed(n / size >= 100 ? 1 : 2).replace(/\.?0+$/, "") + unit;
  }
  return formatInteger(n);
};

const state = {
  snapshot: null, source: "all", tab: "overview", metric: "tokens", session: "",
  sort: "tokens", reverse: true, page: 0, summaries: [], chart: [], focus: -1,
  lastScan: "", today: "", running: false, closed: false, progress: {}, report: null,
};

function element(tag, value = "", className = "") {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
}

function formatDate(date) {
  return date.getFullYear() + "-" + String(date.getMonth() + 1).padStart(2, "0")
    + "-" + String(date.getDate()).padStart(2, "0");
}

function dayDate(key) {
  const [year, month, date] = key.split("-").map(Number);
  return new Date(year, month - 1, date, 12);
}

function shifted(date, offset) {
  const result = dayDate(date);
  result.setDate(result.getDate() + offset);
  return formatDate(result);
}

function projectName(path) {
  return path.split(/[\\/]/).filter(Boolean).at(-1) || path;
}

function sessionName(id) {
  return state.snapshot?.titles[id] || "会话 " + id.split(":").slice(1).join(":").slice(0, 8);
}

function setOptions(select, entries, label) {
  const selected = select.value;
  select.replaceChildren(new Option(label, ""), ...entries.map(([value, name]) => {
    const option = new Option(name, value);
    option.title = value;
    return option;
  }));
  if (entries.some(([value]) => value === selected)) select.value = selected;
}

function syncFilters() {
  const isCustom = $("range").value === "custom";
  for (const label of document.querySelectorAll(".custom-date")) label.hidden = !isCustom;
  const active = (state.snapshot?.rows || []).filter((row) => state.source === "all" || row[source] === state.source);
  const projects = new Set(active.map((row) => row[project]));
  const models = new Set(active.map((row) => row[model]));
  setOptions($("project-filter"), [...projects].sort().map((path) => [path, projectName(path)]), "全部项目");
  setOptions($("model-filter"), [...models].sort().map((name) => [name, name]), "全部模型");
  const sessionFilter = $("session-filter");
  sessionFilter.hidden = !state.session;
  sessionFilter.textContent = state.session ? "× " + sessionName(state.session).slice(0, 32) : "";
  for (const button of document.querySelectorAll("[data-source], [data-tab], [data-metric]")) {
    const type = Object.keys(button.dataset)[0];
    const selected = button.dataset[type] === state[type];
    button.classList.toggle("active", selected);
    button.setAttribute(type === "tab" ? "aria-current" : "aria-pressed", type === "tab" && selected ? "page" : String(selected));
  }
}

function windowDates(rows, end) {
  const range = $("range").value;
  if (range === "custom") return [$("from").value || "2000-01-01", $("to").value || end];
  if (range === "today") return [end, end];
  if (range === "yesterday") {
    const yesterday = shifted(end, -1);
    return [yesterday, yesterday];
  }
  if (range === "week") return [shifted(end, -((dayDate(end).getDay() + 6) % 7)), end];
  if (range !== "all") return [shifted(end, 1 - Number(range)), end];
  if (!rows.length) return [end, end];
  const days = rows.map((row) => row[day]);
  return [days.reduce((a, b) => a < b ? a : b), days.reduce((a, b) => a > b ? a : b)];
}

function emptySummary() {
  return { tokens: 0, fresh: 0, cache: 0, write: 0, output: 0, reasoning: 0, cost: 0,
    unpriced: 0, calls: 0, approximate: 0, latest: "", sources: new Set(), projects: new Set(), models: new Set() };
}

function addRow(summary, row) {
  summary.tokens += amount(row);
  for (const field of ["fresh", "cache", "write", "output", "reasoning", "cost", "unpriced", "calls", "approximate"])
    summary[field] += row[COLUMNS[field]];
  if (row[day] > summary.latest) summary.latest = row[day];
  summary.sources.add(row[source]);
  summary.projects.add(row[project]);
  summary.models.add(row[model]);
}

function costLabel(summary) {
  if (summary.tokens === 0 || summary.unpriced === summary.tokens) return "—";
  return displayUSD(summary.cost) + (summary.unpriced > 0 ? " *" : "");
}

function summaryDimension() {
  return { projects: project, sessions: session }[state.tab] ?? model;
}

function render() {
  syncFilters();
  const [title, details, noun] = TAB[state.tab];
  $("view-hero").dataset.view = state.tab;
  $("view-title").textContent = title;
  $("table-title").textContent = details;
  $("entity-head").textContent = noun;
  $("search").placeholder = "搜索" + noun;
  const all = state.snapshot?.rows || [];
  const dimensions = all.filter((row) =>
    (state.source === "all" || row[source] === state.source)
    && (!$("model-filter").value || row[model] === $("model-filter").value)
    && (!$("project-filter").value || row[project] === $("project-filter").value)
    && (!state.session || row[session] === state.session));
  state.today = formatDate(new Date());
  const [start, end] = windowDates(dimensions, state.today);
  const rows = dimensions.filter((row) => row[day] >= start && row[day] <= end);
  const totals = emptySummary();
  const platforms = Object.fromEntries(Object.keys(SOURCES).map((id) => [id, emptySummary()]));
  const dates = new Set();
  const sessions = new Set();
  const groups = new Map();
  const missingModels = new Set();
  const dimension = summaryDimension();
  for (const row of rows) {
    addRow(totals, row);
    addRow(platforms[row[source]], row);
    if (row[unpriced]) missingModels.add(row[model]);
    sessions.add(row[session]);
    dates.add(row[day]);
    const key = row[dimension];
    if (!groups.has(key)) groups.set(key, { id: key, ...emptySummary() });
    addRow(groups.get(key), row);
  }
  state.report = { rows, totals, platforms, start, end };
  setKPIs(totals, sessions, dates);
  renderSources(platforms, totals);
  renderSourceHealth();
  renderNotice(totals, start, end, missingModels);
  for (const id of Object.keys(SOURCES)) $("legend-" + id).hidden = state.source !== "all" && state.source !== id;
  $("chart-caption").textContent = start <= end ? start + " 至 " + end + (state.metric === "cost" ? " · 仅累计有单价的用量" : "") : "";
  $("trend-heading").textContent = state.metric === "cost" ? "每日费用估算" : "每日用量";
  prepareChart(rows, start, end);
  for (const item of groups.values()) {
    item.name = dimension === session ? sessionName(item.id) : dimension === project ? projectName(item.id) : item.id;
  }
  state.summaries = [...groups.values()];
  renderTable();
}

function setKPIs(totals, sessions, dates) {
  const hasData = !!state.snapshot;
  $("total-tokens").textContent = hasData ? compact(totals.tokens) : "—";
  $("tokens-info").textContent = totals.tokens ? formatInteger(totals.tokens) + " tokens" : "";
  const costButton = $("total-cost");
  costButton.textContent = costLabel(totals);
  costButton.disabled = !totals.tokens;
  costButton.setAttribute("aria-label", "查看当前范围的费用明细 " + costButton.textContent);
  const checked = state.snapshot?.pricing?.checkedAt;
  $("pricing-checked").textContent = checked ? "· 价格表核对于 " + checked : "";
  $("coverage").textContent = totals.tokens
    ? "价格覆盖 " + percent((totals.tokens - totals.unpriced) / totals.tokens) : "";
  const input = totals.fresh + totals.cache + totals.write;
  $("cache-rate").textContent = input ? percent(totals.cache / input) : "—";
  $("cache-info").textContent = input ? compact(totals.cache) + " 缓存读取 / " + compact(input) + " 输入" : "";
  $("sessions-count").textContent = hasData ? formatInteger(sessions.size) : "—";
  $("days-count").textContent = hasData ? dates.size + " 个活跃日" : "";
}

function renderSources(platforms, totals) {
  const parts = [];
  for (const [id, name] of Object.entries(SOURCES)) {
    const heading = element("div", "", "source-amount");
    const label = element("span");
    label.append(element("i", "", "dot " + id), document.createTextNode(name));
    const values = platforms[id];
    const fraction = totals.tokens ? values.tokens / totals.tokens : 0;
    const value = element("strong", compact(values.tokens));
    value.append(element("small", percent(fraction), "source-share"));
    heading.append(label, value);
    const bar = element("progress", "", id);
    bar.max = 1;
    bar.value = fraction;
    bar.setAttribute("aria-label", name + " token 占比");
    const item = element("div");
    item.append(heading, bar);
    parts.push(item);
  }
  $("source-composition").replaceChildren(...parts);
}

function renderSourceHealth() {
  const health = [element("strong", "本机数据源")];
  const attempt = state.progress.error ? state.progress.sources : null;
  for (const [id, name] of Object.entries(SOURCES)) {
    const scan = attempt?.[id] || state.snapshot?.sources[id];
    const label = element("span", name);
    const messages = [];
    let detail = "等待首次扫描";
    if (scan) {
      if (scan.state === "missing") detail = "找不到数据目录";
      else if (scan.state === "unreadable") detail = "目录读取失败";
      else if (scan.files) detail = scan.read + " / " + scan.files + " 个文件";
      else detail = scan.state === "ok" ? "目录内没有会话文件" : "未发现记录";
      if (scan.errors) messages.push(scan.errors + " 个文件读取失败");
      if (scan.retained) messages.push(scan.retained + " 个文件沿用旧结果");
    }
    const row = element("div", "", "health-row");
    row.append(label, element("small", detail));
    if (messages.length) row.append(element("small", messages.join(" · "), "health-warning"));
    const scannedAt = scan?.scannedAt || (scan ? state.lastScan : "");
    if (scannedAt) row.append(element("small", "扫描于 " + localDateTime.format(new Date(scannedAt)), "health-meta"));
    if (scan?.files && !scan.errors && !attempt && (state.source === "all" || state.source === id)
        && !state.report?.platforms[id].tokens) {
      row.append(element("small", "当前筛选范围内无用量", "health-meta"));
    }
    health.push(row);
  }
  $("source-health").replaceChildren(...health);
}

function renderNotice(totals, start, end, missingModels) {
  const messages = [];
  if (start > end) messages.push("开始日期晚于结束日期。");
  const names = [...missingModels].sort();
  if (totals.unpriced) {
    const extra = names.length > 3 ? " 等 " + names.length + " 个模型" : "";
    messages.push(compact(totals.unpriced) + " tokens 缺少单价（" + names.slice(0, 3).join("、") + extra + "）。费用只汇总已定价部分。");
  }
  if (totals.approximate) messages.push(compact(totals.approximate) + " tokens 的日期使用会话或文件时间。");
  const notice = $("notice");
  notice.textContent = messages.join(" ");
  notice.title = names.length > 3 ? names.join("、") : "";
  notice.hidden = messages.length === 0;
}

function summaryList() {
  const query = $("search").value.toLocaleLowerCase().trim();
  return state.summaries.filter((item) => !query || [item.name, item.id, ...item.projects, ...item.models]
    .some((value) => value.toLocaleLowerCase().includes(query))).sort((a, b) => {
    const key = state.sort;
    const order = key === "latest" ? a.latest.localeCompare(b.latest) : a[key] - b[key];
    return order * (state.reverse ? -1 : 1) || a.name.localeCompare(b.name);
  });
}

function renderTable() {
  const items = summaryList();
  const limit = 15;
  const pages = Math.max(1, Math.ceil(items.length / limit));
  state.page = Math.max(0, Math.min(pages - 1, state.page));
  $("table-count").textContent = items.length + " 条";
  $("page-number").textContent = state.page + 1 + " / " + pages;
  $("prev").disabled = state.page === 0;
  $("next").disabled = state.page + 1 >= pages;
  $("export").disabled = items.length === 0;
  $("table-subtitle").textContent = {
    models: "同一模型的思考档位合并统计",
    sessions: "点会话查看它的用量趋势",
  }[state.tab] || "点一行缩小统计范围";
  for (const button of document.querySelectorAll("button[data-sort]"))
    button.parentElement.setAttribute("aria-sort", button.dataset.sort === state.sort ? (state.reverse ? "descending" : "ascending") : "none");
  const body = $("table-body");
  body.replaceChildren();
  if (!items.length) {
    const row = element("tr");
    const text = element("td", state.running && !state.snapshot ? "正在建立本地索引…" : "当前范围内没有匹配的用量。", "empty");
    text.colSpan = 7;
    row.append(text);
    body.append(row);
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of items.slice(state.page * limit, (state.page + 1) * limit)) {
    const row = element("tr");
    const cell = element("td");
    const button = element("button", item.name, "entity");
    button.title = state.tab === "projects" ? item.id : item.name;
    button.addEventListener("click", () => {
      if (state.tab === "projects") $("project-filter").value = item.id;
      else if (state.tab === "sessions") state.session = item.id;
      else $("model-filter").value = item.id;
      state.page = 0;
      $("search").value = "";
      render();
    });
    const subtitle = [...item.sources].map((id) => SOURCES[id]).join(" · ");
    const description = state.tab === "sessions"
      ? [subtitle, [...item.projects].map(projectName).join(" / "), [...item.models].join(" / ")].join(" · ")
      : subtitle;
    const note = element("small", description, "entity-note");
    note.title = description;
    cell.append(button, note);
    row.append(cell);
    for (const field of ["tokens", "fresh", "cache", "output"]) {
      const quantity = element("td", formatInteger(item[field]), "num");
      if (field === "output") quantity.title = "其中推理 " + formatInteger(item.reasoning) + " tokens";
      if (field === "cache") quantity.title = "另有缓存写入 " + formatInteger(item.write) + " tokens";
      row.append(quantity);
    }
    const estimate = element("td", "", "num" + (item.unpriced ? " partial" : ""));
    const costButton = element("button", costLabel(item), "cost-link");
    costButton.type = "button";
    costButton.setAttribute("aria-haspopup", "dialog");
    costButton.setAttribute("aria-controls", "cost-dialog");
    costButton.setAttribute("aria-label", "查看 " + item.name + " 的费用明细 " + costButton.textContent);
    if (item.unpriced) costButton.title = formatInteger(item.unpriced) + " tokens 缺价";
    costButton.addEventListener("click", () => openCost(costButton, item));
    estimate.append(costButton);
    row.append(estimate, element("td", item.latest, "num"));
    fragment.append(row);
  }
  body.append(fragment);
}

function prepareChart(rows, start, end) {
  state.focus = -1;
  $("chart-tip").hidden = true;
  state.chart = [];
  if (start > end) return drawChart();
  const grouped = new Map();
  const daySpan = Math.round((dayDate(end) - dayDate(start)) / 86400000);
  const weekly = daySpan > 170;
  const bucket = (date) => weekly ? shifted(date, -((dayDate(date).getDay() + 6) % 7)) : date;
  const step = weekly ? 7 : 1;
  if (daySpan > 36500) return drawChart();
  for (let date = bucket(start); date <= end; date = shifted(date, step)) {
    grouped.set(date, { date, codex: emptySummary(), agy: emptySummary() });
  }
  for (const row of rows) if (grouped.has(bucket(row[day]))) addRow(grouped.get(bucket(row[day]))[row[source]], row);
  state.chart = [...grouped.values()];
  if (weekly) {
    $("trend-heading").textContent = state.metric === "cost" ? "每周费用估算" : "每周用量";
    $("chart-caption").textContent += " · 每周一分组";
  }
  drawChart();
}

function chartBounds(width, height) {
  return { top: 13, left: 58, bottom: height - 29, right: width - 13 };
}

function drawChart() {
  const canvas = $("chart");
  const { width, height } = canvas.getBoundingClientRect();
  if (width < 100) return;
  const density = window.devicePixelRatio || 1;
  const pixelWidth = Math.round(width * density);
  const pixelHeight = Math.round(height * density);
  if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
  if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(pixelWidth / width, 0, 0, pixelHeight / height, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const series = state.chart;
  const field = state.metric;
  const sums = series.map((item) => item.codex[field] + item.agy[field]);
  const maximum = Math.max(0, ...sums);
  if (!maximum) {
    ctx.fillStyle = chartColors["chart-axis"];
    ctx.textAlign = "center";
    ctx.font = "13px " + CHART_TYPE;
    ctx.fillText(state.running && !state.snapshot ? "正在扫描日志…" : field === "cost" ? "当前范围没有可绘制的估算费用" : "当前范围没有用量", width / 2, height / 2);
    return;
  }
  const magnitude = 10 ** Math.floor(Math.log10(maximum / 4));
  const increment = [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]
    .map((factor) => factor * magnitude).find((tick) => tick * 4 >= maximum);
  const { top, left, bottom, right } = chartBounds(width, height);
  const span = right - left, stride = span / series.length;
  const y = (value) => bottom - value / (4 * increment) * (bottom - top);
  ctx.font = "11.5px " + CHART_TYPE;
  ctx.textBaseline = "middle";
  ctx.textAlign = "right";
  if (state.focus >= 0) {
    ctx.fillStyle = chartColors["chart-focus"];
    ctx.fillRect(left + state.focus * stride, top, stride, bottom - top);
  }
  for (let i = 0; i <= 4; i++) {
    const value = increment * i;
    const pos = y(value);
    const physicalScale = pixelHeight / height;
    ctx.fillStyle = chartColors[i === 0 ? "chart-baseline" : "chart-grid"];
    ctx.fillRect(left, Math.floor(pos * physicalScale) / physicalScale, span, 1 / physicalScale);
    ctx.fillStyle = chartColors["chart-axis"];
    ctx.fillText(field === "cost" ? "$" + Number(value.toPrecision(3)) : compact(Math.round(value)), left - 8, pos);
  }
  const shownLabels = Math.max(2, Math.min(series.length, Math.floor(span / 76)));
  const labeled = new Set(Array.from({ length: shownLabels }, (_, i) => Math.round(i * (series.length - 1) / (shownLabels - 1))));
  const barWidth = Math.min(26, stride * .72);
  ctx.textAlign = "center";
  for (let i = 0; i < series.length; i++) {
    const x = left + (i + .5) * stride;
    let sum = 0;
    const visibleSeries = Object.keys(SOURCES).filter((id) => series[i][id][field] > 0);
    for (const [index, id] of visibleSeries.entries()) {
      const point = series[i][id];
      const value = point[field];
      ctx.fillStyle = chartColors[id];
      const barHeight = Math.max(1, y(sum) - y(sum + value));
      const radius = index === visibleSeries.length - 1 ? Math.min(3, barWidth / 2, barHeight / 2) : 0;
      ctx.beginPath();
      ctx.roundRect(x - barWidth / 2, y(sum) - barHeight, barWidth, barHeight, [radius, radius, 0, 0]);
      ctx.fill();
      sum += value;
    }
    if (labeled.has(i)) {
      ctx.fillStyle = chartColors["chart-axis"];
      ctx.fillText(series[i].date.slice(5), Math.max(left + 13, Math.min(right - 13, x)), bottom + 16);
    }
  }
}

function chartFocus(index) {
  const point = state.chart[index];
  if (!point) return;
  state.focus = index;
  drawChart();
  const width = $("chart").getBoundingClientRect().width;
  const { left, right } = chartBounds(width, 0);
  const x = left + (index + .5) * (right - left) / state.chart.length;
  const tooltip = $("chart-tip");
  tooltip.hidden = false;
  tooltip.textContent = point.date + "\n" + Object.entries(SOURCES).filter(([id]) => state.source === "all" || state.source === id).map(([id, name]) => name + "   "
    + (state.metric === "tokens" ? formatInteger(point[id].tokens) : costLabel(point[id]))).join("\n");
  tooltip.style.left = Math.max(0, Math.min(width - tooltip.offsetWidth, x + 12)) + "px";
}

function costGroups(rows, plans) {
  const groups = new Map();
  for (const row of rows) {
    for (const [id, ...counts] of row[charges]) {
      const key = row[source] + ":" + id;
      if (!groups.has(key)) groups.set(key, { plan: plans[id], source: row[source], amounts: [0, 0, 0, 0] });
      const group = groups.get(key);
      counts.forEach((count, i) => { group.amounts[i] += count; });
    }
  }
  for (const group of groups.values()) {
    group.cost = 0;
    group.unpriced = 0;
    group.priced = 0;
    group.plan.rates.forEach((rate, i) => {
      if (rate === null) group.unpriced += group.amounts[i];
      else {
        group.priced += group.amounts[i];
        group.cost += group.amounts[i] * rate / 1e6;
      }
    });
  }
  return [...groups.values()].sort((a, b) => b.cost - a.cost || b.unpriced - a.unpriced);
}

function rateGroup(group, sources) {
  const { plan, amounts } = group;
  const details = element("details", "", "rate-group");
  const summary = element("summary");
  const text = element("span", "", "rate-model");
  const name = plan.model || plan.recordedModel;
  text.append(element("strong", name));
  const subtitle = [SOURCES[group.source]];
  if (plan.model && plan.recordedModel !== name) subtitle.push("记录模型 " + plan.recordedModel);
  if (plan.threshold) subtitle.push(plan.longContext ? "长上下文" : "标准上下文");
  text.append(element("small", subtitle.join(" · ")));
  const value = element("span", "", "rate-value");
  value.append(element("strong", group.priced ? detailedUSD.format(group.cost) : "—"));
  if (group.unpriced) value.append(element("small", formatInteger(group.unpriced) + " Token 缺价", "partial"));
  summary.append(text, value);

  const table = element("table", "", "rate-table");
  const head = table.createTHead().insertRow();
  for (const [i, label] of ["计量类型", "Token", "单价 USD / 百万", "小计 USD"].entries()) {
    const th = element("th", label, i ? "num" : "");
    th.scope = "col";
    head.append(th);
  }
  const body = table.createTBody();
  for (const [i, label] of ["非缓存输入", "缓存读取", "缓存写入", "输出（含推理）"].entries()) {
    const rate = plan.rates[i];
    const row = body.insertRow();
    row.append(element("td", label), element("td", formatInteger(amounts[i]), "num"),
      element("td", rate === null ? "—" : detailedUSD.format(rate), "num"),
      element("td", rate === null ? (amounts[i] ? "缺价" : "—") : detailedUSD.format(amounts[i] * rate / 1e6),
        "num" + (rate === null && amounts[i] ? " partial" : "")));
  }
  const meta = element("div", "", "rate-meta");
  if (plan.effectiveFrom) meta.append(element("span", plan.effectiveFrom + " 起适用"));
  else if (plan.effectiveBefore) meta.append(element("span", plan.effectiveBefore + " 前适用"));
  if (plan.threshold) meta.append(element("span", "单次输入 " + (plan.longContext ? "> " : "≤ ") + formatInteger(plan.threshold)));
  if (!plan.model) meta.append(element("span", "内置价格表未匹配到计价模型"));
  else {
    const provider = name.startsWith("gemini-") ? "google" : name.startsWith("claude-") ? "anthropic" : "openai";
    const link = element("a", "官方定价 ↗");
    link.href = sources[provider];
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.title = "在默认浏览器打开";
    meta.append(link);
  }
  details.append(summary, table, meta);
  return details;
}

const costDialog = $("cost-dialog");
let costOpener;

function openCost(opener, item) {
  const { rows, totals, start, end } = state.report;
  const scope = item || totals;
  const selected = item ? rows.filter((row) => row[summaryDimension()] === item.id) : rows;
  const pricing = state.snapshot.pricing;
  const timeRange = start === end ? start : start + " 至 " + end;
  $("cost-scope").textContent = [item?.name || "当前筛选", SOURCES[state.source] || "全部来源", timeRange].join(" · ");
  $("cost-amount").textContent = scope.unpriced === scope.tokens ? "—" : detailedUSD.format(scope.cost);
  $("cost-priced").textContent = formatInteger(scope.tokens - scope.unpriced);
  $("cost-unpriced").textContent = formatInteger(scope.unpriced);
  $("cost-note").textContent = pricing
    ? "小计 = Token × 单价 ÷ 1,000,000。标准文本 API 等价估算，价格表核对于 " + pricing.checkedAt + "。"
    : "当前显示上次保存的报表。索引重建完成后，即可查看模型、单价和计费项。";
  const groups = pricing ? costGroups(selected, pricing.plans).map((group) => rateGroup(group, pricing.sources)) : [];
  if (groups.length) groups[0].open = true;
  $("cost-rates").replaceChildren(...groups);
  costOpener = opener;
  costDialog.showModal();
}

$("total-cost").addEventListener("click", (event) => openCost(event.currentTarget));
costDialog.addEventListener("close", () => {
  const returnTo = costOpener?.isConnected ? costOpener : $("total-cost");
  returnTo.focus({ preventScroll: true });
});

function exportCSV() {
  const label = TAB[state.tab][2];
  const columns = [label, "来源", "总 Token", "非缓存输入", "缓存读取", "缓存写入", "输出（含推理）", "推理",
    "API 等价估算 USD（已定价部分）", "缺价 Token", "计量事件数", "最近活动"];
  const quote = (value) => {
    const raw = String(value);
    const safe = typeof value === "string" && /^[=+\-@\t\r\n]/.test(raw) ? "'" + raw : raw;
    return '"' + safe.replaceAll('"', '""') + '"';
  };
  const lines = [columns, ...summaryList().map((item) => [
    item.name, [...item.sources].map((id) => SOURCES[id]).join(" / "), item.tokens, item.fresh, item.cache,
    item.write, item.output, item.reasoning, item.unpriced === item.tokens ? "" : item.cost.toFixed(6),
    item.unpriced, item.calls, item.latest,
  ])];
  const objectUrl = URL.createObjectURL(new Blob(["\ufeff", lines.map((row) => row.map(quote).join(",")).join("\r\n")],
    { type: "text/csv;charset=utf-8" }));
  const link = element("a");
  link.href = objectUrl;
  link.download = "agent-usage-" + state.tab + ".csv";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}

async function request(path, method = "GET") {
  const response = await fetch("./api/" + path, { method, cache: "no-store" });
  if (!response.ok) throw new Error("HTTP " + response.status);
  return response.json();
}

const themePicker = $("appearance");
const themeDialog = $("theme-dialog");
const availableThemes = new Set(root.dataset.themes.split(" "));
const previewThemes = new Set(root.dataset.themePreviews.split(" "));
const themeChoices = new Map();
for (const button of themeDialog.querySelectorAll("[data-theme-choice]")) {
  const id = button.dataset.themeChoice;
  if (!availableThemes.has(id)) {
    button.remove();
    continue;
  }
  themeChoices.set(id, button);
  const sample = button.querySelector(".theme-sample");
  if (!previewThemes.has(id)) {
    sample.hidden = true;
    continue;
  }
  const image = new Image();
  image.alt = "";
  image.loading = "lazy";
  image.src = "./local-art/previews/" + id + ".png";
  sample.append(image);
}

function applyAppearance(theme) {
  root.dataset.theme = theme;
  const name = themeChoices.get(theme).querySelector(".theme-name").textContent;
  $("appearance-current").textContent = name;
  themePicker.setAttribute("aria-label", "界面主题，当前：" + name);
  for (const [id, button] of themeChoices) button.setAttribute("aria-pressed", String(id === theme));
  const style = getComputedStyle(root);
  for (const name of ["codex", "agy", "chart-axis", "chart-focus", "chart-grid", "chart-baseline"]) {
    chartColors[name] = style.getPropertyValue("--" + name).trim();
  }
  window.chrome?.webview?.postMessage("theme:" + theme);
  drawChart();
}

themePicker.hidden = themeChoices.size < 2;
applyAppearance(root.dataset.theme);
let themeSave = Promise.resolve();
themePicker.addEventListener("click", () => {
  themeDialog.showModal();
  themeChoices.get(root.dataset.theme).focus({ preventScroll: true });
});
themeDialog.addEventListener("close", () => themePicker.focus({ preventScroll: true }));
for (const [theme, button] of themeChoices) {
  button.addEventListener("click", () => {
    applyAppearance(theme);
    themeDialog.close();
    $("appearance-status").textContent = "";
    themeSave = themeSave.then(() => request("theme/" + theme, "POST")).catch(() => {
      if (root.dataset.theme === theme) $("appearance-status").textContent = "主题已切换，未能保存为默认。";
    });
  });
}

function status(progress) {
  state.progress = progress;
  state.running = !!progress.running;
  $("refresh").disabled = state.running || state.closed;
  $("quit").disabled = state.running || state.closed;
  const counts = progress.total ? " " + progress.done + " / " + progress.total + " 个文件" : "";
  if (progress.running) $("as-of").textContent = (progress.rebuilding ? "正在重建索引，仍可查看旧报表…" : "正在扫描本机日志…") + counts;
  else if (progress.error) $("as-of").textContent = progress.error;
  else if (state.lastScan) $("as-of").textContent = "更新于 " + localDateTime.format(new Date(state.lastScan));
  else $("as-of").textContent = "等待首次扫描";
  renderSourceHealth();
}

async function loadData() {
  const data = await request("data");
  state.snapshot = data.snapshot;
  state.lastScan = data.scannedAt || "";
  status(data.progress);
  if (!$("from").value) $("from").value = shifted(data.snapshot?.today || formatDate(new Date()), -29);
  if (!$("to").value) $("to").value = data.snapshot?.today || formatDate(new Date());
  render();
}

async function poll() {
  if (state.closed) return;
  try {
    const reply = await request("status");
    if ((reply.scannedAt || "") !== state.lastScan || (state.running && !reply.progress.running))
      await loadData();
    else {
      status(reply.progress);
      if (state.today && state.today !== formatDate(new Date())) render();
    }
  } catch {
    $("as-of").textContent = "本地服务暂时无法连接，请重新打开应用。";
  }
  if (!state.closed) setTimeout(poll, state.running ? 700 : 2200);
}

for (const button of document.querySelectorAll("[data-source], [data-tab], [data-metric]")) {
  button.addEventListener("click", () => {
    const type = Object.keys(button.dataset)[0];
    state[type] = button.dataset[type];
    if (type === "source") state.session = "";
    if (type === "tab") $("search").value = "";
    state.page = 0;
    render();
  });
}
for (const control of ["range", "from", "to", "model-filter", "project-filter"]) {
  $(control).addEventListener("change", () => { state.page = 0; render(); });
}
$("search").addEventListener("input", () => { state.page = 0; renderTable(); });
$("session-filter").addEventListener("click", () => { state.session = ""; render(); });
$("clear").addEventListener("click", () => {
  state.source = "all"; state.session = ""; state.page = 0;
  for (const id of ["model-filter", "project-filter", "search"]) $(id).value = "";
  $("range").value = "30";
  render();
});
for (const button of document.querySelectorAll("button[data-sort]")) {
  button.addEventListener("click", () => {
    state.reverse = state.sort === button.dataset.sort ? !state.reverse : true;
    state.sort = button.dataset.sort;
    state.page = 0;
    renderTable();
  });
}
$("prev").addEventListener("click", () => { state.page--; renderTable(); });
$("next").addEventListener("click", () => { state.page++; renderTable(); });
$("export").addEventListener("click", exportCSV);
$("refresh").addEventListener("click", async () => {
  status({ running: true });
  try {
    await request("refresh", "POST");
  } catch { status({ running: false, error: "刷新未启动，请重试。" }); }
});
$("quit").addEventListener("click", async () => {
  try {
    await themeSave;
    await request("quit", "POST");
    state.closed = true;
    $("as-of").textContent = "应用已退出。可关闭此页面，或双击程序重新打开。";
    $("quit").disabled = $("refresh").disabled = true;
    window.chrome?.webview?.postMessage("quit");
  } catch { $("as-of").textContent = "扫描进行中，结束后可退出。"; }
});
$("chart").addEventListener("pointermove", (event) => {
  if (!state.chart.length) return;
  const bounds = $("chart").getBoundingClientRect();
  const { left, right } = chartBounds(bounds.width, bounds.height);
  const fraction = (event.clientX - bounds.left - left) / (right - left);
  chartFocus(Math.min(state.chart.length - 1, Math.max(0, Math.floor(fraction * state.chart.length))));
});
function clearChartFocus() {
  state.focus = -1;
  $("chart-tip").hidden = true;
  drawChart();
}
$("chart").addEventListener("pointerleave", clearChartFocus);
$("chart").addEventListener("blur", clearChartFocus);
$("chart").addEventListener("keydown", (event) => {
  if (!state.chart.length || !["ArrowLeft", "ArrowRight", "Escape"].includes(event.key)) return;
  event.preventDefault();
  if (event.key === "Escape") clearChartFocus();
  else chartFocus(Math.max(0, Math.min(state.chart.length - 1, state.focus + (event.key === "ArrowLeft" ? -1 : 1))));
});
new ResizeObserver(() => drawChart()).observe($("chart"));
function watchPixelDensity() {
  matchMedia("(resolution: " + window.devicePixelRatio + "dppx)").addEventListener("change", () => {
    drawChart();
    watchPixelDensity();
  }, { once: true });
}
watchPixelDensity();
loadData().catch(() => {}).finally(poll);
