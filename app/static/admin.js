const state = {
  sections: [],
  section: null,
  keywords: [],
  excludes: [],
  jobs: [],
  today: null,
  collectAt: "05:00",
  scheduler: false,
  review: null,
  reviewArticles: [],
  excluded: new Set(),
  orderTouched: false,
};

const el = (id) => document.getElementById(id);
const loginCard = el("loginCard");
const adminMain = el("adminMain");
const jobList = el("jobList");

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function isoDate(value) {
  // KST 날짜는 toISOString()을 거치면 하루 밀린다.
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}

function formatDateTime(value) {
  if (!value) return "-";
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? value : time.toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}) },
    ...options,
  });
  if (response.status === 401) {
    showLogin("세션이 만료되었습니다. 다시 로그인하세요.");
    throw new Error("관리자 인증이 필요합니다.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join(", ") : data.detail;
    throw new Error(detail || `요청에 실패했습니다. (${response.status})`);
  }
  return data;
}

function setMessage(text, isError = false) {
  const box = el("formMessage");
  box.textContent = text;
  box.className = isError ? "hint error" : "hint";
}

/* ── Login ─────────────────────────────────────────────────── */
function showLogin(message = "") {
  loginCard.hidden = false;
  adminMain.hidden = true;
  const error = el("loginError");
  error.hidden = !message;
  error.textContent = message;
}

el("loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/admin/login", { method: "POST", body: JSON.stringify({ key: el("adminKey").value }) });
    el("adminKey").value = "";
    loginCard.hidden = true;
    adminMain.hidden = false;
    await refresh();
  } catch (error) {
    const box = el("loginError");
    box.hidden = false;
    box.textContent = error.message;
  }
});

el("logoutButton").addEventListener("click", async () => {
  await fetch("/api/admin/logout", { method: "POST", credentials: "same-origin" });
  showLogin("로그아웃했습니다.");
});

/* ── Section picker ────────────────────────────────────────── */
function currentSection() {
  return state.sections.find((section) => section.key === state.section) || null;
}

function renderSections() {
  el("sectionPicker").innerHTML = state.sections
    .map(
      (section) => `
      <label class="section-option ${section.key === state.section ? "selected" : ""}">
        <input type="radio" name="section" value="${escapeHtml(section.key)}" ${section.key === state.section ? "checked" : ""} />
        <span class="section-option-name">${escapeHtml(section.label)}</span>
        <span class="section-option-desc">${escapeHtml(section.description)}</span>
        <span class="section-option-tag">${section.requires_review ? "검토·승인 후 공개" : "수집 즉시 공개"}</span>
        <span class="section-option-tag">${section.has_briefing ? "AI 브리핑 포함" : "기사 스크랩만"}</span>
      </label>`,
    )
    .join("");

  el("sectionPicker").querySelectorAll("input").forEach((input) =>
    input.addEventListener("change", () => {
      state.section = input.value;
      applySectionDefaults();
      renderSections();
      renderAll();
    }),
  );
}

function applySectionDefaults() {
  const section = currentSection();
  if (!section) return;
  state.keywords = [...section.keywords];
  state.excludes = [...section.exclude_keywords];
  el("generateBriefing").checked = section.has_briefing;
  el("generateBriefing").disabled = !section.has_briefing;
}

/* ── Keyword chips ─────────────────────────────────────────── */
function renderChips(container, values, kind, emptyText) {
  container.innerHTML = values.length
    ? values
        .map(
          (keyword) =>
            `<span class="chip on">${escapeHtml(keyword)}<button type="button" data-remove="${escapeHtml(keyword)}" aria-label="${escapeHtml(keyword)} 제거">×</button></span>`,
        )
        .join("")
    : `<span class="hint">${escapeHtml(emptyText)}</span>`;
  container.querySelectorAll("button[data-remove]").forEach((button) =>
    button.addEventListener("click", () => {
      state[kind] = state[kind].filter((value) => value !== button.dataset.remove);
      renderAll();
    }),
  );
}

function renderQueryPreview() {
  const quoted = state.keywords.map((keyword) => `"${keyword}"`);
  const section = currentSection();
  const mode = section ? section.match_mode : "any";
  const included = mode === "all" ? quoted.join(" ") : quoted.length > 1 ? `(${quoted.join(" OR ")})` : quoted.join("");
  const excluded = state.excludes.map((keyword) => `-"${keyword}"`).join(" ");
  const query = `${included} ${excluded}`.trim();
  el("queryPreview").textContent = query ? `검색식 미리보기  ${query}` : "";
}

function renderWindowPreview() {
  const value = el("reportDate").value;
  const box = el("windowPreview");
  if (!value) {
    box.textContent = "";
    box.className = "hint";
    return;
  }
  const day = new Date(`${value}T00:00:00`).getDay();
  if (day === 0 || day === 6) {
    box.textContent = "토요일·일요일은 수집하지 않습니다. 주말 기사는 월요일 수집에 함께 담깁니다.";
    box.className = "hint error";
    el("submitJob").disabled = true;
    el("registerAll").disabled = true;
    return;
  }
  el("submitJob").disabled = false;
  el("registerAll").disabled = false;

  // 월요일은 금·토·일 사흘치를 담는다.
  const start = new Date(`${value}T00:00:00`);
  start.setDate(start.getDate() - (day === 1 ? 3 : 1));
  const overdue = state.today && value <= state.today;
  box.className = "hint";
  box.textContent =
    `수집 시간창 ${isoDate(start)} 09:00 ~ ${value} ${state.collectAt} (KST)` +
    (day === 1 ? " · 월요일은 금·토·일 사흘치" : "") +
    " · " +
    (overdue ? "이미 지난 시간창입니다. 등록 후 ‘지금 실행’을 누르세요." : `${value} ${state.collectAt}에 자동 실행됩니다.`);
}

function renderAll() {
  renderChips(el("selectedChips"), state.keywords, "keywords", "키워드를 1개 이상 추가하세요.");
  renderChips(el("excludeChips"), state.excludes, "excludes", "제외 키워드 없음");
  renderQueryPreview();
  renderWindowPreview();
}

function addFrom(input, kind) {
  const keyword = input.value.trim().replace(/\s+/g, " ");
  if (!keyword) return;
  if (state[kind].length >= 20) {
    setMessage("키워드는 20개까지 지정할 수 있습니다.", true);
    return;
  }
  if (!state[kind].includes(keyword)) state[kind].push(keyword);
  input.value = "";
  renderAll();
}

/* ── Job list ──────────────────────────────────────────────── */
const STATUS = {
  pending: { text: "대기", className: "badge pending" },
  running: { text: "수집 중", className: "badge running" },
  complete: { text: "완료", className: "badge complete" },
  failed: { text: "실패", className: "badge failed" },
};

function jobCard(job) {
  let badge = STATUS[job.status] || STATUS.pending;
  if (job.status === "complete") {
    badge = job.approved
      ? { text: "공개 중", className: "badge complete" }
      : { text: "검토 대기", className: "badge review" };
  }
  const review =
    job.status === "complete"
      ? `<button class="btn ${job.needs_review ? "btn-primary" : ""}" type="button" data-action="review">
           ${job.needs_review ? "스크랩 검토" : "스크랩 확인"}
         </button>
         ${job.approved ? '<button class="btn" type="button" data-action="unapprove">공개 내리기</button>' : ""}`
      : "";
  const error = job.error_message ? `<p class="hint error">${escapeHtml(job.error_message)}</p>` : "";
  return `
    <article class="job-card" data-job="${job.id}">
      <div class="job-main">
        <h3 class="job-title">${escapeHtml(job.section_label || job.name)}<span class="${badge.className}">${badge.text}</span></h3>
        <p class="job-meta">
          키워드 ${escapeHtml(job.keywords.join(", "))}${job.exclude_keywords.length ? ` · 제외 ${escapeHtml(job.exclude_keywords.join(", "))}` : ""}<br />
          전체 ${job.article_count}건 · 중복 제거 ${job.unique_count}건 · 마지막 수집 ${formatDateTime(job.last_run_at)}
          ${job.approved ? ` · 승인 ${formatDateTime(job.approved_at)}` : ""}
        </p>
        ${error}
      </div>
      <div class="job-actions">
        <button class="btn" type="button" data-action="run" ${job.status === "running" ? "disabled" : ""}>지금 실행</button>
        ${review}
        <button class="btn btn-danger" type="button" data-action="delete">삭제</button>
      </div>
    </article>`;
}

function renderJobs() {
  if (!state.jobs.length) {
    jobList.innerHTML = '<div class="empty"><p class="empty-title">등록된 수집이 없습니다</p><p>위에서 기준일과 메뉴를 선택해 등록하세요.</p></div>';
    return;
  }
  const byDate = state.jobs.reduce((result, job) => {
    (result[job.report_date] ||= []).push(job);
    return result;
  }, {});
  jobList.innerHTML = Object.keys(byDate)
    .sort((a, b) => b.localeCompare(a))
    .map((date) => `<section class="job-group"><h4 class="job-group-title">${escapeHtml(date)}</h4>${byDate[date].map(jobCard).join("")}</section>`)
    .join("");

  jobList.querySelectorAll("[data-action]").forEach((button) =>
    button.addEventListener("click", () => {
      const jobId = button.closest(".job-card").dataset.job;
      if (button.dataset.action === "run") runJob(jobId, button);
      if (button.dataset.action === "delete") deleteJob(jobId);
      if (button.dataset.action === "review") openReview(jobId);
      if (button.dataset.action === "unapprove") unapprove(jobId);
    }),
  );
}

/* ── Review & approval ─────────────────────────────────────── */
function setReviewMessage(text, isError = false) {
  const box = el("reviewMessage");
  box.textContent = text;
  box.className = isError ? "hint error" : "hint";
}

function reviewRow(article, index, total) {
  const checked = !state.excluded.has(article.id);
  const body = (article.content || article.summary || "").slice(0, 300);
  return `
    <div class="review-item ${checked ? "" : "dropped"}" data-article="${article.id}">
      <span class="review-rank">${index + 1}</span>
      <label class="review-pick">
        <input type="checkbox" ${checked ? "checked" : ""} aria-label="공개 여부" />
      </label>
      <span class="review-body">
        <span class="review-title">${escapeHtml(article.title)}</span>
        <span class="review-meta">
          ${escapeHtml(article.publisher)} · ${escapeHtml(formatDateTime(article.published_at))}
          ${article.duplicate_of ? '<span class="tag tag-duplicate">중복</span>' : ""}
          ${article.manual ? '<span class="tag tag-manual">직접 추가</span>' : ""}
        </span>
        <span class="review-excerpt">${escapeHtml(body)}</span>
        <a class="text-link" href="${escapeHtml(article.source_url)}" target="_blank" rel="noopener noreferrer">원문 열기</a>
      </span>
      <span class="review-move">
        <button class="btn move" type="button" data-move="up" ${index === 0 ? "disabled" : ""} aria-label="위로">↑</button>
        <button class="btn move" type="button" data-move="down" ${index === total - 1 ? "disabled" : ""} aria-label="아래로">↓</button>
      </span>
    </div>`;
}

function moveArticle(articleId, direction) {
  const from = state.reviewArticles.findIndex((article) => article.id === articleId);
  const to = from + direction;
  if (from < 0 || to < 0 || to >= state.reviewArticles.length) return;
  const [moved] = state.reviewArticles.splice(from, 1);
  state.reviewArticles.splice(to, 0, moved);
  state.orderTouched = true;
  renderReview();
  const item = el("reviewList").querySelector(`[data-article="${articleId}"]`);
  if (item) {
    item.classList.add("just-moved");
    item.querySelector(`[data-move="${direction < 0 ? "up" : "down"}"]`).focus();
  }
}

function updateReviewSummary() {
  const job = state.review;
  if (!job) return;
  const kept = state.reviewArticles.length - state.excluded.size;
  const order = state.orderTouched ? " · 순서 변경됨" : "";
  el("reviewNote").textContent = (job.approved
    ? `공개 중 · ${kept}건 공개, ${state.excluded.size}건 제외. 고친 뒤 다시 승인하면 갱신됩니다.`
    : `${state.reviewArticles.length}건 중 ${kept}건 공개 예정, ${state.excluded.size}건 제외`) + order;
  el("approveButton").textContent = job.approved ? "다시 승인" : "승인하고 공개";
}

function syncReviewChecks() {
  el("reviewList").querySelectorAll(".review-item").forEach((item) => {
    const excluded = state.excluded.has(item.dataset.article);
    item.classList.toggle("dropped", excluded);
    item.querySelector("input").checked = !excluded;
  });
  updateReviewSummary();
}

function renderReview() {
  if (!state.review) {
    el("reviewCard").hidden = true;
    return;
  }
  const job = state.review;
  el("reviewCard").hidden = false;
  el("reviewTitle").textContent = `${job.report_date} · ${job.section_label || job.name} 스크랩 검토`;
  updateReviewSummary();

  const total = state.reviewArticles.length;
  el("reviewList").innerHTML = total
    ? state.reviewArticles.map(function (article, index) { return reviewRow(article, index, total); }).join("")
    : '<p class="empty">검토할 기사가 없습니다.</p>';

  el("reviewList").querySelectorAll("[data-move]").forEach((button) =>
    button.addEventListener("click", () => {
      const item = button.closest(".review-item");
      moveArticle(item.dataset.article, button.dataset.move === "up" ? -1 : 1);
    }),
  );

  el("reviewList").querySelectorAll(".review-item input").forEach((input) =>
    // 목록 전체를 다시 그리면 스크롤 위치와 포커스를 잃는다. 바뀐 줄만 손본다.
    input.addEventListener("change", () => {
      const item = input.closest(".review-item");
      if (input.checked) state.excluded.delete(item.dataset.article);
      else state.excluded.add(item.dataset.article);
      item.classList.toggle("dropped", !input.checked);
      updateReviewSummary();
    }),
  );
}

async function openReview(jobId) {
  try {
    const data = await api(`/api/admin/jobs/${jobId}/articles`);
    state.review = data.job;
    // 서버가 돌려준 차례가 곧 공개 화면의 차례다.
    state.reviewArticles = data.articles;
    state.orderTouched = data.articles.some(function (article) { return article.sort_order > 0; });
    // 처음 검토할 때는 중복 기사만 미리 빼 둔다. 관리자는 여기서 더 뺄 수 있다.
    state.excluded = new Set(
      data.articles
        .filter((article) => (data.job.approved ? article.excluded : article.excluded || article.duplicate_of))
        .map((article) => article.id),
    );
    setReviewMessage("");
    el("linkInput").value = "";
    setLinkMessage("검색에서 빠진 기사는 주소를 붙여 넣어 직접 넣을 수 있습니다.");
    renderReview();
    el("reviewCard").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function approveReview() {
  if (!state.review) return;
  const button = el("approveButton");
  button.disabled = true;
  try {
    const result = await api(`/api/admin/jobs/${state.review.id}/approve`, {
      method: "POST",
      body: JSON.stringify({
        excluded_ids: [...state.excluded],
        ordered_ids: state.reviewArticles.map(function (article) { return article.id; }),
      }),
    });
    setMessage(
      `승인 완료 · ${result.published_count}건 공개, ${result.excluded_count}건 제외` +
        (result.briefing_status === "fallback" ? " (LLM 응답 없음, 확인 목록으로 대체)" : ""),
    );
    state.review = null;
    renderReview();
    await refresh();
  } catch (error) {
    setReviewMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function unapprove(jobId) {
  try {
    await api(`/api/admin/jobs/${jobId}/unapprove`, { method: "POST" });
    setMessage("공개를 내렸습니다. 다시 검토한 뒤 승인하세요.");
    await refresh();
  } catch (error) {
    setMessage(error.message, true);
  }
}

function setLinkMessage(text, isError = false) {
  const box = el("linkMessage");
  box.textContent = text;
  box.className = isError ? "hint error" : "hint";
}

async function addLinkedArticle() {
  if (!state.review) return;
  const input = el("linkInput");
  const url = input.value.trim();
  if (!url) {
    setLinkMessage("기사 주소를 넣어 주세요.", true);
    return;
  }
  const button = el("addLink");
  button.disabled = true;
  setLinkMessage("기사를 읽어 오는 중입니다…");
  try {
    const result = await api(`/api/admin/jobs/${state.review.id}/articles`, {
      method: "POST",
      body: JSON.stringify({ url }),
    });
    const article = result.article;
    const existing = state.reviewArticles.findIndex(function (item) { return item.id === article.id; });
    if (existing >= 0) state.reviewArticles[existing] = article;
    else state.reviewArticles.push(article);
    // 직접 넣은 기사는 공개가 기본이다.
    state.excluded.delete(article.id);
    input.value = "";
    renderReview();
    setLinkMessage(
      (result.already_present ? "이미 있던 기사를 새로 읽었습니다" : "기사를 추가했습니다") +
        ` · ${article.publisher} · ${formatDateTime(article.published_at)}` +
        (result.approved ? " · 승인하고 공개를 다시 누르면 브리핑에도 반영됩니다." : ""),
    );
  } catch (error) {
    setLinkMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
}

el("addLink").addEventListener("click", addLinkedArticle);
el("linkInput").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  addLinkedArticle();
});

el("approveButton").addEventListener("click", approveReview);
el("closeReview").addEventListener("click", () => {
  state.review = null;
  renderReview();
});
el("resetOrder").addEventListener("click", () => {
  // 기본 차례: 지역 매체 먼저, 그다음 최신순.
  state.reviewArticles.sort((a, b) => {
    if (!!b.preferred !== !!a.preferred) return b.preferred - a.preferred;
    return String(b.published_at).localeCompare(String(a.published_at));
  });
  state.orderTouched = false;
  renderReview();
  setReviewMessage("기본 순서로 되돌렸습니다. 승인하면 반영됩니다.");
});

el("selectAll").addEventListener("click", () => {
  state.excluded.clear();
  syncReviewChecks();
});
el("clearAll").addEventListener("click", () => {
  state.excluded = new Set(state.reviewArticles.map((article) => article.id));
  syncReviewChecks();
});

async function refresh() {
  const session = await api("/api/admin/session");
  state.sections = session.sections;
  state.today = session.today;
  state.collectAt = session.collect_at;
  state.scheduler = session.scheduler;

  if (!session.configured) {
    showLogin(".env의 ADMIN_API_KEY가 비어 있습니다. 값을 설정하고 서버를 다시 시작하세요.");
    return;
  }
  if (!session.authenticated) {
    showLogin();
    return;
  }
  loginCard.hidden = true;
  adminMain.hidden = false;

  if (session.pages_url && !el("publishNote").textContent) {
    setPublishNote(`승인 뒤 ‘사이트 게시’를 누르면 ${session.pages_url} 에 반영됩니다.`);
  }
  el("scheduleNote").textContent = state.scheduler
    ? `평일 매일 ${state.collectAt} KST 자동 ${session.auto_register ? "등록·수집" : "수집"}`
    : "스케줄러 꺼짐 · 수동 실행만 가능";

  if (!state.section && state.sections.length) {
    state.section = state.sections[0].key;
    applySectionDefaults();
  }
  renderSections();
  renderAll();

  const data = await api("/api/admin/jobs");
  state.jobs = data.jobs;
  renderJobs();
  if (state.review) {
    const current = state.jobs.find((job) => job.id === state.review.id);
    if (current) state.review = current;
    renderReview();
  }
}

async function runJob(jobId, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "수집 중…";
  try {
    const result = await api(`/api/admin/jobs/${jobId}/run`, { method: "POST" });
    setMessage(
      result.status === "complete"
        ? `수집 완료 · 전체 ${result.article_count}건, 중복 제거 ${result.unique_count}건`
        : `수집 실패 · ${result.error}`,
      result.status !== "complete",
    );
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    button.textContent = original;
    await refresh();
  }
}

async function deleteJob(jobId) {
  const job = state.jobs.find((item) => item.id === jobId);
  if (!window.confirm(`'${job ? job.section_label || job.name : jobId}' 수집과 저장된 기사·사진을 삭제할까요?`)) return;
  try {
    await api(`/api/admin/jobs/${jobId}`, { method: "DELETE" });
    setMessage("삭제했습니다.");
    await refresh();
  } catch (error) {
    setMessage(error.message, true);
  }
}

/* ── Submit ────────────────────────────────────────────────── */
el("jobForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const section = currentSection();
  if (!section) {
    setMessage("메뉴를 선택하세요.", true);
    return;
  }
  if (!state.keywords.length) {
    setMessage("검색 키워드를 1개 이상 추가하세요.", true);
    return;
  }
  const button = el("submitJob");
  button.disabled = true;
  try {
    const data = await api("/api/admin/jobs", {
      method: "POST",
      body: JSON.stringify({
        report_date: el("reportDate").value,
        section: section.key,
        keywords: state.keywords,
        exclude_keywords: state.excludes,
        generate_briefing: el("generateBriefing").checked,
      }),
    });
    setMessage(
      data.job.runnable_now
        ? `'${section.label}' 등록 완료. 시간창이 지났으므로 ‘지금 실행’을 누르세요.`
        : `'${section.label}' 등록 완료. ${data.job.report_date} ${state.collectAt}에 자동 수집합니다.`,
    );
    await refresh();
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
});

el("registerAll").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const data = await api("/api/admin/jobs/bulk", {
      method: "POST",
      body: JSON.stringify({ report_date: el("reportDate").value }),
    });
    const created = data.created.map((job) => job.section_label || job.name);
    setMessage(
      `${created.length ? `등록: ${created.join(", ")}` : "새로 등록된 메뉴가 없습니다"}` +
        (data.skipped.length ? ` · 이미 등록됨: ${data.skipped.join(", ")}` : ""),
    );
    await refresh();
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
});

el("runDueButton").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await api("/api/admin/run-due", { method: "POST" });
    setMessage(`밀린 수집 ${result.job_count}건을 실행했습니다.`);
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    button.disabled = false;
    await refresh();
  }
});

function setPublishNote(text, isError = false) {
  const box = el("publishNote");
  box.textContent = text;
  box.className = isError ? "hint error" : "hint";
}

el("publishButton").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "게시 중…";
  setPublishNote("정적 사이트를 만들어 GitHub에 올리는 중입니다…");
  try {
    const result = await api("/api/admin/publish", { method: "POST" });
    const summary = `${result.dates.length}일치 · 최신 ${result.latest_date || "없음"} · ${result.built_at.replace("T", " ")} 기준`;
    setPublishNote(
      `${result.message} ${summary}` + (result.pages_url ? ` · ${result.pages_url}` : ""),
    );
  } catch (error) {
    setPublishNote(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
});

el("refreshButton").addEventListener("click", () => refresh().catch(() => {}));
el("addKeyword").addEventListener("click", () => addFrom(el("keywordInput"), "keywords"));
el("addExclude").addEventListener("click", () => addFrom(el("excludeInput"), "excludes"));
el("resetKeywords").addEventListener("click", () => {
  applySectionDefaults();
  renderAll();
  setMessage("기본 검색 조건으로 되돌렸습니다.");
});

[["keywordInput", "keywords"], ["excludeInput", "excludes"]].forEach(([id, kind]) =>
  el(id).addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    addFrom(el(id), kind);
  }),
);

el("reportDate").addEventListener("change", renderWindowPreview);

/* ── Initialize ────────────────────────────────────────────── */
async function initialize() {
  try {
    await refresh();
  } catch (error) {
    if (loginCard.hidden && adminMain.hidden) showLogin(error.message);
  }
  const today = state.today || isoDate(new Date());
  const tomorrow = new Date(`${today}T00:00:00`);
  tomorrow.setDate(tomorrow.getDate() + 1);
  el("reportDate").value = isoDate(tomorrow);
  renderWindowPreview();
}

initialize();
