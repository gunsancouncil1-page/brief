const state = {
  menu: [],
  sections: {},
  tab: null,
  today: null,
  reportDate: null,
  overview: {},
  // 공개 화면은 중복을 제거한 목록만 보여 준다.
  view: "unique",
  collectAt: "05:00",
};

const el = (id) => document.getElementById(id);
const menuEl = el("menu");
const dateRail = el("dateRail");
const contentEl = el("content");
const contentTitle = el("contentTitle");
const contentNote = el("contentNote");

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function formatDateTime(value) {
  if (!value) return "-";
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? value : time.toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });
}

function formatDate(value) {
  const time = new Date(`${value}T00:00:00`);
  if (Number.isNaN(time.getTime())) return value;
  return time.toLocaleDateString("ko-KR", { month: "long", day: "numeric", weekday: "short" });
}

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = new Error(data.detail || `요청에 실패했습니다. (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

/* ── Data source ───────────────────────────────────────────────
   같은 화면을 두 곳에서 쓴다.
   · 이 PC의 서버        → /api/... 를 호출한다.
   · GitHub Pages 정적본 → 내보낸 JSON 파일을 읽는다.
   ───────────────────────────────────────────────────────────── */
const STATIC_MODE = document.body.dataset.mode === "static";
const dateCache = new Map();

async function loadDateFile(date) {
  if (!dateCache.has(date)) dateCache.set(date, await api(`./data/${date}.json`));
  return dateCache.get(date);
}

function staticSection(payload, section) {
  const found = payload.sections[section];
  if (!found) {
    const error = new Error("이 날짜에 등록된 수집이 없습니다.");
    error.status = 404;
    throw error;
  }
  return found;
}

const source = STATIC_MODE
  ? {
      menu: () => api("./data/index.json"),
      overview: async (date) => {
        const payload = await loadDateFile(date);
        const jobs = {};
        Object.entries(payload.sections).forEach(([key, value]) => {
          jobs[key] = { ...value, status: "complete" };
        });
        return { jobs };
      },
      articles: async (date, section) => ({ articles: staticSection(await loadDateFile(date), section).articles }),
      briefing: async (date, section) => {
        const found = staticSection(await loadDateFile(date), section);
        if (!found.briefing) {
          const error = new Error("아직 생성된 브리핑이 없습니다.");
          error.status = 404;
          throw error;
        }
        return { briefing: found.briefing };
      },
    }
  : {
      menu: () => api("/api/menu"),
      overview: (date) => api(`/api/reports/${date}`),
      articles: (date, section, view) => api(`/api/reports/${date}/${section}/articles?view=${view}`),
      briefing: (date, section) => api(`/api/reports/${date}/${section}/briefing`),
    };

function currentTab() {
  return state.menu.find((tab) => tab.key === state.tab) || state.menu[0] || null;
}

function currentJob() {
  const tab = currentTab();
  return tab ? state.overview[tab.section] || null : null;
}

/* ── Chrome ────────────────────────────────────────────────── */
function renderMenu() {
  menuEl.innerHTML = state.menu
    .map((tab) => {
      const job = state.overview[tab.section];
      const ready = job && job.approved && (tab.view === "articles" || job.generate_briefing);
      return `<button type="button" role="tab" class="nav-tab ${tab.key === state.tab ? "active" : ""} ${ready ? "has-data" : ""}"
        data-tab="${tab.key}" aria-selected="${tab.key === state.tab}">${escapeHtml(tab.label)}<span class="nav-dot"></span></button>`;
    })
    .join("");
  menuEl.querySelectorAll("button").forEach((button) =>
    button.addEventListener("click", () => {
      state.tab = button.dataset.tab;
      state.view = "unique";
      renderMenu();
      render();
    }),
  );
}

function renderDates() {
  // 공개 화면은 가장 최근 수집분만 보여 준다. 지난 날짜는 관리자 페이지에서 확인한다.
  if (!state.reportDate) {
    dateRail.innerHTML = '<span class="hint">아직 수집된 자료가 없습니다.</span>';
    return;
  }
  const stale = state.today && state.reportDate !== state.today;
  dateRail.innerHTML =
    `<span class="date-chip active">${escapeHtml(state.reportDate)}</span>` +
    (stale ? '<span class="hint">가장 최근 수집분입니다.</span>' : "");
}

function renderNote(job) {
  if (!job) {
    contentNote.textContent = "";
    contentNote.className = "card-note";
    return;
  }
  if (!job.approved) {
    contentNote.textContent = "";
    contentNote.className = "card-note";
    return;
  }
  contentNote.textContent = `${job.report_date} 주요검색결과 · ${job.published_count}건`;
  contentNote.className = "card-note";
}

function showSkeleton() {
  contentEl.innerHTML = `
    <div class="skeleton-block">
      <div class="skeleton skeleton-line"></div>
      <div class="skeleton skeleton-line"></div>
      <div class="skeleton skeleton-line"></div>
    </div>`;
}

function showEmpty(title, message) {
  contentEl.innerHTML = `<div class="empty"><p class="empty-title">${escapeHtml(title)}</p><p>${escapeHtml(message)}</p></div>`;
  contentEl.classList.add("fade-in");
}

/* ── Articles ─────────────────────────────────────────────── */
function renderArticles(articles) {
  if (!articles.length) {
    const tab = currentTab();
    showEmpty(
      "보도자료가 없습니다",
      `${state.reportDate} 수집 시간창에는 ${tab ? tab.label : "해당 메뉴의"} 보도자료가 없습니다.`,
    );
    return;
  }
  // 관리자가 정한 차례를 그대로 보여 준다(서버가 그 순서로 내려 준다).
  contentEl.innerHTML = `
    <ol class="article-list">
      ${articles
        .map(
          (article) => `
        <li class="article">
          <h4 class="article-title">
            <a href="${escapeHtml(article.source_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(article.title)}</a>
          </h4>
          <p class="article-meta">
            <span class="article-publisher">${escapeHtml(article.publisher)}</span>
            <span>${escapeHtml(formatDateTime(article.published_at))}</span>
          </p>
        </li>`,
        )
        .join("")}
    </ol>`;

  contentEl.classList.add("fade-in");
}

/* ── Briefing ──────────────────────────────────────────────── */
// 로컬 LLM은 요청한 형식을 항상 지키지는 않는다(#, ###, -, *, 1., **굵게**).
// 어떤 조합이 오더라도 읽을 수 있게 관대하게 해석한다.
const HEADING_MARK = /^#{1,6}\s*/;
const BULLET_MARK = /^\s*([-*•]|\d+[.)])\s+/;

function inlineMarkdown(text) {
  return escapeHtml(text).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}

function renderBriefingMarkdown(markdown) {
  const blocks = String(markdown || "").split(/\n\s*\n/).filter((block) => block.trim());
  let ledeUsed = false;
  return blocks
    .map((block) => {
      const lines = block.split("\n").filter((line) => line.trim());
      let html = "";
      if (HEADING_MARK.test(lines[0])) {
        html += `<h3>${inlineMarkdown(lines.shift().replace(HEADING_MARK, ""))}</h3>`;
      }
      if (!lines.length) return html;

      const bullets = lines.filter((line) => BULLET_MARK.test(line));
      if (bullets.length === lines.length) {
        return `${html}<ul>${lines.map((line) => `<li>${inlineMarkdown(line.replace(BULLET_MARK, ""))}</li>`).join("")}</ul>`;
      }
      const text = lines.map((line) => inlineMarkdown(line)).join("<br>");
      if (!html && !ledeUsed) {
        ledeUsed = true;
        return `<p class="briefing-lede">${text}</p>`;
      }
      return `${html}<p>${text}</p>`;
    })
    .join("");
}

/* ── Render ────────────────────────────────────────────────── */
async function render() {
  const tab = currentTab();
  if (!tab) return;
  const job = currentJob();

  contentTitle.textContent = tab.label;
  renderNote(job);

  if (!state.reportDate) {
    showEmpty("수집된 자료가 없습니다", "관리자 페이지에서 기준일을 등록하면 이곳에 표시됩니다.");
    return;
  }
  if (!job) {
    showEmpty("이 날짜에 등록된 수집이 없습니다", `관리자 페이지에서 '${tab.label}' 수집을 등록하세요.`);
    return;
  }
  if (!job.approved) {
    const waiting = {
      pending: ["수집 대기 중", `${state.reportDate} ${state.collectAt}에 자동으로 수집합니다.`],
      running: ["수집 중", "수집이 끝나면 관리자 검토를 거쳐 공개됩니다."],
      failed: ["수집 실패", job.error_message || "관리자 페이지에서 원인을 확인하세요."],
      complete: ["승인 대기 중", "수집은 끝났습니다. 관리자 검토·승인 후 이곳에 기사가 올라갑니다."],
    };
    const [title, message] = waiting[job.status] || waiting.pending;
    showEmpty(title, message);
    return;
  }

  showSkeleton();
  try {
    if (tab.view === "briefing") {
      const data = await source.briefing(state.reportDate, tab.section);
      const fallback =
        data.briefing.status === "fallback"
          ? '<p class="notice warning">로컬 LLM 응답을 받지 못해 확인 목록으로 대체되었습니다.</p>'
          : "";
      contentEl.innerHTML = `${fallback}<article class="briefing">${renderBriefingMarkdown(data.briefing.body)}</article>`;
      contentEl.classList.add("fade-in");
    } else {
      const data = await source.articles(state.reportDate, tab.section, state.view);
      renderArticles(data.articles);
    }
  } catch (error) {
    if (error.status === 404) {
      showEmpty("아직 준비되지 않았습니다", error.message);
      return;
    }
    contentEl.innerHTML = `<p class="error-block">${escapeHtml(error.message)}</p>`;
  }
}

async function loadOverview() {
  state.overview = {};
  if (state.reportDate) {
    try {
      const data = await source.overview(state.reportDate);
      Object.entries(data.jobs).forEach(([section, job]) => {
        if (job) state.overview[section] = job;
      });
    } catch (error) {
      contentEl.innerHTML = `<p class="error-block">${escapeHtml(error.message)}</p>`;
    }
  }
  renderMenu();
  await render();
}

async function initialize() {
  try {
    const menu = await source.menu();
    state.menu = menu.menu;
    state.sections = menu.sections;
    state.collectAt = menu.collect_at;
    state.tab = menu.menu[0].key;
    state.today = menu.today;
    state.reportDate = menu.latest_date;
    state.builtAt = menu.built_at || null;
  } catch (error) {
    contentEl.innerHTML = `<p class="error-block">${escapeHtml(error.message)}</p>`;
    return;
  }

  renderDates();
  await loadOverview();

  const stamp = el("buildStamp");
  if (stamp && state.builtAt) stamp.textContent = `${state.builtAt.replace("T", " ")} 기준`;
}

initialize();
