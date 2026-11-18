(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  const railBadge = document.getElementById("rail-badge");
  let tab = "batches";
  let meta = null;
  let batches = [];
  let batchesRail = null;
  let selected = null;
  let detail = null;
  let reports = [];
  let railsData = { current: null, rails: [], log: [] };
  let closeRuns = [];
  let replayRail = null;
  let form = { object_key: "", content_hash: "", byte_size: "100" };

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || j.detail || ("HTTP " + r.status));
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  function railText(rail) {
    if (!rail) return "";
    return `现行轨：${rail.rail_code} · ${rail.rule_version} · 门槛 ≥${rail.min_byte_size} 字节`;
  }

  function syncBadge() {
    const rail = (meta && meta.rail) || null;
    railBadge.textContent = rail ? railText(rail) + "（全局唯一）" : "";
  }

  async function refresh() {
    meta = await api("/api/meta");
    document.getElementById("lab-brand").textContent = meta.lab_name || "湖区清单台";
    const b = await api("/api/batches");
    batches = b.items || [];
    batchesRail = b.rail || null;
    reports = (await api("/api/visibility-reports")).items || [];
    railsData = await api("/api/rails");
    const cr = await api("/api/close-runs");
    closeRuns = cr.items || [];
    replayRail = cr.rail || null;
    syncBadge();
  }

  async function openBatch(id) {
    selected = id;
    detail = await api("/api/batches/" + id);
    tab = "detail";
    syncNav();
    render();
  }

  async function doRegister() {
    if (!selected) return;
    setMsg("");
    try {
      await api("/api/batches/" + selected + "/objects", {
        method: "POST",
        body: JSON.stringify({
          object_key: form.object_key,
          content_hash: form.content_hash,
          byte_size: Number(form.byte_size || 0),
        }),
      });
      form = { object_key: "", content_hash: "", byte_size: "100" };
      await openBatch(selected);
      await refresh();
      setMsg("已登记对象");
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doClose() {
    if (!selected) return;
    setMsg("");
    try {
      await api("/api/batches/" + selected + "/close", { method: "POST", body: "{}" });
      await openBatch(selected);
      await refresh();
      setMsg("清单已闭合");
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doReport() {
    setMsg("");
    try {
      const r = await api("/api/visibility-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date: "2026-11-16" }),
      });
      await refresh();
      tab = "replay";
      syncNav();
      setMsg(r.report.summary + "｜" + railText(r.rail));
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doSwitch(toRail) {
    setMsg("");
    try {
      const r = await api("/api/rails/switch", {
        method: "POST",
        body: JSON.stringify({ to_rail: toRail }),
      });
      await refresh();
      setMsg("现行轨已切换为 " + r.rail.rail_code + "（" + r.rail.rule_version + "）");
      render();
    } catch (e) {
      await refresh();
      setMsg(e.message);
      render();
    }
  }

  function syncNav() {
    document.querySelectorAll("nav [data-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
  }

  function renderBatches() {
    return `
      <h2>落地批次</h2>
      <p class="muted">主链：选批 → 登记对象 → 闭合清单 → 可见性对账</p>
      <p class="muted">批次卡片读轨：${railText(batchesRail)}</p>
      <table>
        <thead><tr><th>编号</th><th>湖区</th><th>业务日</th><th>状态</th><th>现行轨</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${b.id}</td>
            <td>${b.zone_name}<div class="muted">${b.zone_code}</div></td>
            <td>${b.biz_date}</td>
            <td><span class="pill">${b.status}</span></td>
            <td><span class="pill rail">${batchesRail ? batchesRail.rail_code : "-"}</span></td>
            <td><button type="button" class="secondary" data-open="${b.id}">打开</button></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderDetail() {
    if (!detail) {
      return `<h2>批次详情</h2><p class="muted">请先从落地批次打开一条。</p>`;
    }
    const b = detail.batch;
    const objs = detail.objects || [];
    const rail = detail.rail;
    return `
      <h2>批次详情 #${b.id}</h2>
      <p>${b.zone_name} · ${b.biz_date} · <span class="pill">${b.status}</span></p>
      <p class="muted">对象详情读轨：${railText(rail)}（登记按此门槛校验）</p>
      <div class="row-actions">
        <button type="button" id="btn-close">闭合清单</button>
        <button type="button" class="secondary" id="btn-report-from-detail">跑可见性对账</button>
      </div>
      <div class="panel">
        <h3>登记对象</h3>
        <label>对象键<input id="f-key" value="${form.object_key}" /></label>
        <label>内容指纹<input id="f-hash" value="${form.content_hash}" /></label>
        <label>字节数<input id="f-size" value="${form.byte_size}" /></label>
        <button type="button" id="btn-register">写入登记</button>
      </div>
      <h3>对象列表</h3>
      <table>
        <thead><tr><th>键</th><th>指纹</th><th>字节</th><th>状态</th></tr></thead>
        <tbody>
          ${objs
            .map(
              (o) => `<tr>
            <td>${o.object_key}</td><td>${o.content_hash}</td><td>${o.byte_size}</td><td>${o.status}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderRails() {
    const cur = railsData.current;
    const rails = railsData.rails || [];
    const log = railsData.log || [];
    return `
      <h2>规则队列</h2>
      <p class="muted">现行轨凭证：旁路表单行指针，全局唯一；切换一次事务落成，失败留痕。</p>
      <p>当前现行：<span class="pill rail">${cur ? cur.rail_code + " · " + cur.rule_version + " · 门槛 ≥" + cur.min_byte_size + " 字节" : "无"}</span></p>
      <table>
        <thead><tr><th>轨号</th><th>规则版本</th><th>灌库门槛</th><th>现行</th><th></th></tr></thead>
        <tbody>
          ${rails
            .map(
              (r) => `<tr>
            <td>${r.rail_code}</td>
            <td>${r.rule_version}</td>
            <td>≥${r.min_byte_size} 字节</td>
            <td>${r.is_current ? '<span class="pill rail">现行</span>' : ""}</td>
            <td>${
              r.is_current
                ? ""
                : `<button type="button" class="secondary" data-switch="${r.rail_code}">切到此轨</button>`
            }</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      <h3>切换登记（含失败因）</h3>
      <table>
        <thead><tr><th>编号</th><th>时间</th><th>从</th><th>到</th><th>结果</th><th>说明 / 失败因</th><th>操作人</th></tr></thead>
        <tbody>
          ${log
            .map(
              (l) => `<tr>
            <td>${l.id}</td><td>${l.created_at}</td><td>${l.from_rail || "-"}</td><td>${l.to_rail || "-"}</td>
            <td>${l.ok ? '<span class="pill rail">成功</span>' : '<span class="pill danger">失败</span>'}</td>
            <td>${l.reason}</td><td>${l.operator}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderReplay() {
    return `
      <h2>清洗回放台</h2>
      <p class="muted">回放台读轨：${railText(replayRail)}；每条闭合记录留有所跑轨号，可与现行轨对账。</p>
      <h3>清洗闭合记录</h3>
      <table>
        <thead><tr><th>编号</th><th>批次</th><th>对象数</th><th>轨号</th><th>规则版本</th><th>门槛</th><th>时间</th></tr></thead>
        <tbody>
          ${closeRuns
            .map(
              (r) => `<tr>
            <td>${r.id}</td>
            <td>#${r.batch_id} ${r.zone_name}<div class="muted">${r.zone_code} · ${r.biz_date}</div></td>
            <td>${r.object_count}</td>
            <td><span class="pill rail">${r.rail_code || "-"}</span></td>
            <td>${r.rule_version || "-"}</td>
            <td>≥${r.min_byte_size} 字节</td>
            <td>${r.created_at}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      <h3>可见性对账</h3>
      <button type="button" id="btn-report">生成对账</button>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>错位</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${r.id}</td><td>${r.as_of_date}</td><td>${r.mismatch_count}</td><td>${r.summary}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function render() {
    if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "detail") view.innerHTML = renderDetail();
    else if (tab === "rails") view.innerHTML = renderRails();
    else view.innerHTML = renderReplay();

    view.querySelectorAll("[data-open]").forEach((btn) => {
      btn.onclick = () => openBatch(Number(btn.dataset.open));
    });
    view.querySelectorAll("[data-switch]").forEach((btn) => {
      btn.onclick = () => doSwitch(btn.dataset.switch);
    });
    const reg = document.getElementById("btn-register");
    if (reg) {
      const key = document.getElementById("f-key");
      const hash = document.getElementById("f-hash");
      const size = document.getElementById("f-size");
      key.oninput = () => (form.object_key = key.value);
      hash.oninput = () => (form.content_hash = hash.value);
      size.oninput = () => (form.byte_size = size.value);
      reg.onclick = doRegister;
    }
    const closeBtn = document.getElementById("btn-close");
    if (closeBtn) closeBtn.onclick = doClose;
    const r1 = document.getElementById("btn-report");
    const r2 = document.getElementById("btn-report-from-detail");
    if (r1) r1.onclick = doReport;
    if (r2) r2.onclick = doReport;
  }

  document.querySelectorAll("nav [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      syncNav();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(render)
    .catch((e) => setMsg(e.message));
})();
