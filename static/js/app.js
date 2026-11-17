(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let meta = null;
  let batches = [];
  let selected = null;
  let detail = null;
  let reports = [];
  let exports = [];
  let landings = [];
  let appendix = null;
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

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  async function refresh() {
    meta = await api("/api/meta");
    document.getElementById("lab-brand").textContent = meta.lab_name || "湖区清单台";
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/visibility-reports")).items || [];
    exports = (await api("/api/exports")).items || [];
    landings = (await api("/api/downstream-landings")).items || [];
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

  async function doExport() {
    if (!selected) return;
    setMsg("");
    try {
      const r = await api("/api/batches/" + selected + "/export", {
        method: "POST",
        body: JSON.stringify({ credential: "manifest_package" }),
      });
      await openBatch(selected);
      await refresh();
      setMsg("已生成不可变清单包：" + r.job.target_name);
      render();
    } catch (e) {
      await refresh();
      setMsg(e.message);
      render();
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
      tab = "reports";
      syncNav();
      setMsg(r.report.summary);
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function openAppendix(jobId) {
    setMsg("");
    try {
      const r = await api("/api/exports/" + jobId + "/appendix");
      appendix = r.appendix;
      render();
    } catch (e) {
      appendix = null;
      setMsg(e.message);
      render();
    }
  }

  async function doLand(jobId) {
    setMsg("");
    try {
      const r = await api("/api/exports/" + jobId + "/land", {
        method: "POST",
        body: "{}",
      });
      await refresh();
      setMsg(
        "已解析落库 " +
          r.landing.object_count +
          " 个指纹，包：" +
          r.landing.target_name
      );
      render();
    } catch (e) {
      setMsg(e.message);
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
      <p class="muted">主链：选批 → 登记对象 → 闭合清单 → 可见性对账 → 清单导出 → 下游落库</p>
      <table>
        <thead><tr><th>编号</th><th>湖区</th><th>业务日</th><th>状态</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${b.id}</td>
            <td>${esc(b.zone_name)}<div class="muted">${esc(b.zone_code)}</div></td>
            <td>${esc(b.biz_date)}</td>
            <td><span class="pill">${esc(b.status)}</span></td>
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
    const chip = detail.export
      ? `<span class="chip chip-ok" data-appendix="${detail.export.id}">已导出 · ${esc(detail.export.target_name)}</span>`
      : b.status === "已闭合"
      ? `<span class="chip chip-idle">未导出</span>`
      : "";
    return `
      <h2>批次详情 #${b.id}</h2>
      <p>${esc(b.zone_name)} · ${esc(b.biz_date)} · <span class="pill">${esc(b.status)}</span> ${chip}</p>
      <div class="row-actions">
        <button type="button" id="btn-close">闭合清单</button>
        <button type="button" class="secondary" id="btn-export">导出清单（不可变包）</button>
        <button type="button" class="secondary" id="btn-report-from-detail">跑可见性对账</button>
      </div>
      <div class="panel">
        <h3>登记对象</h3>
        <label>对象键<input id="f-key" value="${esc(form.object_key)}" /></label>
        <label>内容指纹<input id="f-hash" value="${esc(form.content_hash)}" /></label>
        <label>字节数<input id="f-size" value="${esc(form.byte_size)}" /></label>
        <button type="button" id="btn-register">写入登记</button>
      </div>
      <h3>对象列表</h3>
      <table>
        <thead><tr><th>键</th><th>指纹</th><th>字节</th><th>状态</th></tr></thead>
        <tbody>
          ${objs
            .map(
              (o) => `<tr>
            <td>${esc(o.object_key)}</td><td>${esc(o.content_hash)}</td><td>${o.byte_size}</td><td>${esc(o.status)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderReports() {
    return `
      <h2>可见性对账</h2>
      <button type="button" id="btn-report">生成对账</button>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>错位</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${r.id}</td><td>${esc(r.as_of_date)}</td><td>${r.mismatch_count}</td><td>${esc(r.summary)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function statusChipClass(status) {
    if (status === "已生成") return "chip-ok";
    if (status === "已拦截") return "chip-block";
    return "chip-fail";
  }

  function renderAppendix() {
    if (!appendix) {
      return `<div class="panel muted">点击队列中的「指纹附页」查看导出包内冻结的指纹集合。</div>`;
    }
    const j = appendix.job;
    if (!appendix.verified) {
      return `<div class="panel"><span class="chip chip-block">${esc(j.status)}</span><p class="muted">${esc(j.error || "该任务无可用附页")}</p></div>`;
    }
    const objs = appendix.objects || [];
    return `
      <div class="panel">
        <h3>指纹附页 · ${esc(j.target_name)}</h3>
        <p class="muted">
          凭证 ${esc(j.credential)} · 包 sha256 <code>${esc(j.package_sha256.slice(0, 16))}…</code>
          · 指纹集 sha256 <code>${esc(j.fingerprint_sha256.slice(0, 16))}…</code>
          · 生成于 ${esc(appendix.generated_at)}
        </p>
        <table>
          <thead><tr><th>对象键</th><th>内容指纹</th><th>字节</th></tr></thead>
          <tbody>
            ${objs
              .map(
                (o) => `<tr><td>${esc(o.object_key)}</td><td>${esc(o.content_hash)}</td><td>${o.byte_size}</td></tr>`
              )
              .join("")}
          </tbody>
        </table>
      </div>`;
  }

  function renderExports() {
    const landedIds = new Set(landings.map((l) => l.export_job_id));
    return `
      <h2>导出队列</h2>
      <p class="muted">凭证定边：物化不可变清单包（manifest_package）；每批仅一份成功产物，包名即下游拉取句柄。</p>
      <table>
        <thead><tr><th>编号</th><th>批次</th><th>凭证</th><th>状态</th><th>包名/表名</th><th>对象数</th><th>说明</th><th></th></tr></thead>
        <tbody>
          ${exports
            .map(
              (j) => `<tr>
            <td>${j.id}</td>
            <td>#${j.batch_id}</td>
            <td>${esc(j.credential)}</td>
            <td><span class="chip ${statusChipClass(j.status)}">${esc(j.status)}</span></td>
            <td>${j.target_name ? esc(j.target_name) : '<span class="muted">—</span>'}</td>
            <td>${j.object_count}</td>
            <td class="muted">${esc(j.error || "")}</td>
            <td class="cell-actions">
              <button type="button" class="secondary" data-appendix="${j.id}">指纹附页</button>
              ${
                j.status === "已生成"
                  ? landedIds.has(j.id)
                    ? '<span class="chip chip-ok">已落库</span>'
                    : `<button type="button" data-land="${j.id}">解析落库</button>`
                  : ""
              }
            </td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      <h3>下游本机落库</h3>
      <table>
        <thead><tr><th>落库编号</th><th>来源任务</th><th>包名</th><th>指纹数</th><th>包 sha256</th></tr></thead>
        <tbody>
          ${landings
            .map(
              (l) => `<tr>
            <td>${l.id}</td><td>#${l.export_job_id}（批次 #${l.batch_id}）</td>
            <td>${esc(l.target_name)}</td><td>${l.object_count}</td>
            <td><code>${esc(l.package_sha256.slice(0, 16))}…</code></td>
          </tr>`
            )
            .join("") || '<tr><td colspan="5" class="muted">尚无落库记录</td></tr>'}
        </tbody>
      </table>
      ${renderAppendix()}`;
  }

  function render() {
    if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "detail") view.innerHTML = renderDetail();
    else if (tab === "reports") view.innerHTML = renderReports();
    else view.innerHTML = renderExports();

    view.querySelectorAll("[data-open]").forEach((btn) => {
      btn.onclick = () => openBatch(Number(btn.dataset.open));
    });
    view.querySelectorAll("[data-appendix]").forEach((btn) => {
      btn.onclick = () => {
        tab = "exports";
        syncNav();
        openAppendix(Number(btn.dataset.appendix));
      };
    });
    view.querySelectorAll("[data-land]").forEach((btn) => {
      btn.onclick = () => doLand(Number(btn.dataset.land));
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
    const exportBtn = document.getElementById("btn-export");
    if (exportBtn) exportBtn.onclick = doExport;
    const r1 = document.getElementById("btn-report");
    const r2 = document.getElementById("btn-report-from-detail");
    if (r1) r1.onclick = doReport;
    if (r2) r2.onclick = doReport;
  }

  document.querySelectorAll("nav [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      if (tab !== "exports") appendix = null;
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
