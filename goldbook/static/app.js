(() => {
  const csrf = (element) => element.dataset.csrf || "";
  const result = document.querySelector("#creator-result");
  document.querySelector("#creator-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const source = form.elements.source.value;
    const response = await fetch("/api/creators", {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf(form)}, body: JSON.stringify({source})});
    const payload = await response.json();
    if (!result) return;
    if (!payload.ok) {
      result.value = payload.error.message;
      return;
    }
    const progress = document.createElement("a");
    progress.href = `/creators/${encodeURIComponent(payload.data.creator_uid)}`;
    progress.textContent = "查看处理进度";
    result.replaceChildren("已加入后台发现队列。", progress);
    form.reset();
  });
  document.querySelectorAll(".sync-creator").forEach((form) => form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!window.confirm("重新发现该 UP 主最近公开视频？")) return;
    await fetch(`/api/creators/${encodeURIComponent(form.dataset.uid)}/sync`, {method: "POST", headers: {"X-CSRF-Token": csrf(form)}});
    window.location.reload();
  }));
  const chart = document.querySelector("#price-chart");
  if (chart && window.Chart) {
    const chartData = JSON.parse(chart.dataset.chart || "{}");
    const prices = chartData.prices || [];
    const markers = chartData.markers || [];
    const markerDatasets = markers.map((marker) => ({label: `${marker.bvid} 标记`, data: [marker.publication, marker.entry, marker.exit_1d, marker.exit_5d, marker.exit_20d].filter(Boolean).map((date) => ({x: date, y: prices.find((price) => price.date === date)?.close})).filter((point) => point.y !== undefined), showLine: false, pointRadius: 5, pointBackgroundColor: "#b91c1c"}));
    if (prices.length) new Chart(chart, {type: "line", data: {labels: prices.map((p) => p.date), datasets: [{label: "XAU/USD 收盘价", data: prices.map((p) => p.close), borderColor: "#9a6b16", borderWidth: 2, pointRadius: 0, tension: .15}, ...markerDatasets]}, options: {responsive: true, maintainAspectRatio: false, interaction: {mode: "index", intersect: false}, scales: {x: {title: {display: true, text: "交易日"}, ticks: {autoSkip: true, maxTicksLimit: 8, maxRotation: 0}}, y: {title: {display: true, text: "XAU/USD（美元/盎司）"}, ticks: {callback: (value) => `$${Number(value).toLocaleString("zh-CN")}`}}}, plugins: {legend: {labels: {usePointStyle: true}}, tooltip: {callbacks: {label: (context) => `${context.dataset.label}: $${Number(context.parsed.y).toFixed(2)}/oz`}}}}});
  }
  const claimCanvas = document.querySelector("#claim-price-chart");
  if (claimCanvas && window.Chart) {
    const chartData = JSON.parse(claimCanvas.dataset.chart || "{}");
    const allPrices = chartData.all_prices || chartData.prices || [];
    const focusPrices = chartData.prices || [];
    const claims = chartData.claims || [];
    const hourly = chartData.granularity === "1h";
    if (hourly) {
      const help = claimCanvas.closest("section")?.querySelector(".chart-help");
      if (help) help.textContent = "虚线为目标点位；圆点为发布、首根完整小时线、首次命中与截止时刻。横轴为上海时间（小时），纵轴为美元/盎司。";
      const allButton = claimCanvas.closest("section")?.querySelector('[data-chart-view="all"]');
      if (allButton) allButton.textContent = "全部小时线";
    }
    const keyOf = (price) => price.at || price.date;
    const labelOf = (price) => hourly ? new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}).format(new Date(price.at)) : price.date;
    let viewStart = Math.max(0, allPrices.findIndex((price) => keyOf(price) === keyOf(focusPrices[0] || {})));
    let viewEnd = Math.max(viewStart + 1, allPrices.findIndex((price) => keyOf(price) === keyOf(focusPrices.at(-1) || {})));
    if (!focusPrices.length) { viewStart = 0; viewEnd = Math.max(0, allPrices.length - 1); }
    const datasetsFor = (prices) => {
      const levels = claims.flatMap((claim, claimIndex) => (claim.levels || []).map((level, levelIndex) => ({label: `目标 ${Number(level).toFixed(2)}`, data: prices.map((price) => (!claim.window_start || keyOf(price) >= claim.window_start) && (!claim.window_end || keyOf(price) <= claim.window_end) ? level : null), borderColor: ["#c2413b", "#176b87", "#7451a6"][(claimIndex + levelIndex) % 3], borderDash: [7, 5], borderWidth: 1.5, pointRadius: 0})));
      const hits = claims.filter((claim) => claim.first_hit && prices.some((price) => keyOf(price) === claim.first_hit)).map((claim) => ({label: "首次命中", data: prices.map((price) => keyOf(price) === claim.first_hit ? price.close : null), showLine: false, pointRadius: 6, pointHoverRadius: 8, pointBackgroundColor: "#16805d"}));
      const markerNames = {publication: "视频发布", entry: "首根完整小时线", hit: "首次命中", deadline: "观察截止"};
      const markers = (chartData.markers || []).map((marker) => {
        const index = prices.findIndex((price) => keyOf(price) >= marker.at);
        return {label: markerNames[marker.kind] || marker.kind, data: prices.map((price, priceIndex) => priceIndex === index ? price.close : null), showLine: false, pointRadius: marker.kind === "hit" ? 6 : 4, pointBackgroundColor: marker.kind === "hit" ? "#16805d" : "#684f2a"};
      }).filter((dataset) => dataset.data.some((value) => value !== null));
      return [{label: "收盘价", data: prices.map((price) => price.close), borderColor: "#9a6b16", backgroundColor: "rgba(154,107,22,.10)", fill: true, borderWidth: 2.5, pointRadius: 0, tension: .18}, ...levels, ...hits, ...markers];
    };
    const currentPrices = () => allPrices.slice(viewStart, viewEnd + 1);
    const chart = new Chart(claimCanvas, {type: "line", data: {labels: currentPrices().map(labelOf), datasets: datasetsFor(currentPrices())}, options: {responsive: true, maintainAspectRatio: false, animation: {duration: 180}, interaction: {mode: "index", intersect: false}, scales: {x: {title: {display: true, text: chartData.axis?.x_title || "交易日"}, ticks: {autoSkip: true, maxTicksLimit: hourly ? 12 : 8, maxRotation: 0}}, y: {title: {display: true, text: chartData.axis?.y_title || "XAU/USD（美元/盎司）"}, ticks: {callback: (value) => `$${Number(value).toLocaleString("zh-CN")}`}}}, plugins: {legend: {position: "bottom", labels: {usePointStyle: true, boxWidth: 8}}, tooltip: {callbacks: {title: (items) => items[0]?.label || "", label: (context) => `${context.dataset.label}: $${Number(context.parsed.y).toFixed(2)}/oz`}}}}});
    const refresh = () => { const prices = currentPrices(); chart.data.labels = prices.map(labelOf); chart.data.datasets = datasetsFor(prices); chart.update(); };
    const setView = (kind) => { if (kind === "all") { viewStart = 0; viewEnd = Math.max(0, allPrices.length - 1); } else { viewStart = Math.max(0, allPrices.findIndex((price) => keyOf(price) === keyOf(focusPrices[0] || {}))); viewEnd = Math.max(viewStart + 1, allPrices.findIndex((price) => keyOf(price) === keyOf(focusPrices.at(-1) || {}))); } refresh(); };
    document.querySelectorAll("[data-chart-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.chartView)));
    document.querySelector("[data-chart-reset]")?.addEventListener("click", () => setView("focus"));
    claimCanvas.addEventListener("wheel", (event) => { event.preventDefault(); const span = viewEnd - viewStart + 1; if ((event.deltaY > 0 && span >= allPrices.length) || (event.deltaY < 0 && span <= 5)) return; const change = event.deltaY > 0 ? 2 : -2; viewStart = Math.max(0, viewStart - Math.ceil(change / 2)); viewEnd = Math.min(allPrices.length - 1, viewEnd + Math.floor(change / 2)); refresh(); }, {passive: false});
    let dragX = null;
    claimCanvas.addEventListener("pointerdown", (event) => { dragX = event.clientX; claimCanvas.setPointerCapture(event.pointerId); claimCanvas.classList.add("is-dragging"); });
    claimCanvas.addEventListener("pointermove", (event) => { if (dragX === null) return; const step = Math.round((dragX - event.clientX) / 35); if (!step) return; const span = viewEnd - viewStart; const nextStart = Math.max(0, Math.min(allPrices.length - span - 1, viewStart + step)); viewStart = nextStart; viewEnd = nextStart + span; dragX = event.clientX; refresh(); });
    const stopDrag = () => { dragX = null; claimCanvas.classList.remove("is-dragging"); };
    claimCanvas.addEventListener("pointerup", stopDrag); claimCanvas.addEventListener("pointercancel", stopDrag);
  }
  const backtestCanvas = document.querySelector("#backtest-chart");
  if (backtestCanvas && window.Chart) {
    const data = JSON.parse(backtestCanvas.dataset.chart || "{}");
    const equity = data.equity || [];
    const gold = data.gold || [];
    const positions = data.positions || [];
    const labels = [...new Set([...gold.map((point) => point.date), ...equity.map((point) => point.date)])].sort();
    const byDate = (rows, field) => {
      const values = new Map(rows.map((row) => [row.date, row[field]]));
      return labels.map((date) => values.has(date) ? values.get(date) : null);
    };
    const positionAt = (date) => positions.find((row) => row.start_date <= date && date <= row.end_date);
    const positionStatusBand = {
      id: "positionStatusBand",
      beforeDraw(chart) {
        const {ctx, chartArea, scales} = chart;
        if (!chartArea || !scales.x) return;
        ctx.save();
        ctx.beginPath();
        ctx.rect(chartArea.left, chartArea.top, chartArea.width, chartArea.height);
        ctx.clip();
        positions.forEach((position) => {
          const start = labels.findIndex((date) => date >= position.start_date);
          const end = labels.findLastIndex((date) => date <= position.end_date);
          if (start < 0 || end < start) return;
          const pixel = (index) => scales.x.getPixelForValue(index);
          const left = start === 0 ? chartArea.left : (pixel(start - 1) + pixel(start)) / 2;
          const right = end === labels.length - 1 ? chartArea.right : (pixel(end) + pixel(end + 1)) / 2;
          const bandHeight = 9;
          const bandTop = chartArea.bottom - bandHeight;
          ctx.fillStyle = position.kind === "long" ? "rgba(22,128,93,.72)" : position.kind === "cash" ? "rgba(98,108,120,.55)" : "rgba(189,63,58,.72)";
          ctx.fillRect(left, bandTop, right - left, bandHeight);
        });
        ctx.restore();
      },
    };
    if (equity.length) new Chart(backtestCanvas, {
      type: "line",
      plugins: [positionStatusBand],
      data: {
        labels,
        datasets: [
          {label: "账户余额", data: byDate(equity, "balance"), yAxisID: "yBalance", borderColor: "#0f6f50", backgroundColor: "transparent", spanGaps: true, borderWidth: 3, pointRadius: equity.length < 30 ? 2 : 0, tension: .18},
          {label: "XAU/USD 收盘价", data: byDate(gold, "close"), yAxisID: "yGold", borderColor: "#b37a16", backgroundColor: "transparent", spanGaps: true, borderWidth: 2, pointRadius: 0, tension: .12},
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {mode: "index", intersect: false},
        scales: {
          x: {title: {display: true, text: "交易日"}, ticks: {autoSkip: true, maxTicksLimit: 10, maxRotation: 0}},
          yBalance: {position: "left", title: {display: true, text: data.axes?.balance || "账户余额（美元）"}, ticks: {callback: (value) => `$${Number(value).toFixed(2)}`}},
          yGold: {position: "right", grid: {drawOnChartArea: false}, title: {display: true, text: data.axes?.gold || "XAU/USD（美元/盎司）"}, ticks: {callback: (value) => `$${Number(value).toLocaleString("zh-CN")}`}},
        },
        plugins: {
          legend: {position: "bottom", labels: {usePointStyle: true}},
          tooltip: {callbacks: {
            label: (context) => context.dataset.yAxisID === "yGold" ? `黄金：$${Number(context.parsed.y).toFixed(2)}/oz` : `余额：$${Number(context.parsed.y).toFixed(2)}`,
            afterBody: (items) => {
              const position = positionAt(items[0]?.label);
              return position ? [`${position.direction_label} · ${position.title}`, `该段收益 ${(position.stage_return * 100).toFixed(2)}%`] : [];
            },
          }},
        },
      },
    });
  }
  const jobBody = document.querySelector("#jobs-table tbody");
  const renderJobs = (jobs) => {
    jobBody.replaceChildren();
    if (!jobs.length) {
      const cell = document.createElement("td"); cell.colSpan = 6; cell.textContent = "暂无任务。";
      const row = document.createElement("tr"); row.append(cell); jobBody.append(row); return;
    }
    jobs.forEach((job) => {
      const row = document.createElement("tr");
      [job.kind, job.status, job.stage, `${Math.round(job.progress * 100)}%`, job.error || "—", "请刷新页面以操作"].forEach((value) => {
        const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
      });
      jobBody.append(row);
    });
  };
  const pollJobs = async () => {
    try { const response = await fetch("/api/jobs"); const payload = await response.json(); if (payload.ok) renderJobs(payload.data); } catch (_) { /* Static table remains usable. */ }
  };
  if (jobBody) { window.setInterval(pollJobs, 10000); }
})();
