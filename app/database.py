from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


JOB_STATUSES = ("pending", "running", "complete", "failed")

# The category-based schema was replaced by admin-defined jobs. A pre-existing
# database is renamed aside instead of being dropped.
_LEGACY_TABLES = ("articles", "report_runs", "briefings")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _retire_legacy_schema(self, conn: sqlite3.Connection) -> None:
        existing = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "articles" not in existing:
            return
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        if "job_id" in columns:
            return
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        for table in _LEGACY_TABLES:
            if table in existing:
                conn.execute(f'ALTER TABLE "{table}" RENAME TO "{table}_legacy_{stamp}"')

    @staticmethod
    def _add_missing_columns(conn: sqlite3.Connection) -> None:
        """이전 버전에서 만들어진 표에 새 열을 채워 넣는다."""
        additions = {
            ("article_images", "caption"): "TEXT NOT NULL DEFAULT ''",
            ("jobs", "section"): "TEXT NOT NULL DEFAULT 'council'",
            ("jobs", "approved_at"): "TEXT",
            ("articles", "excluded"): "INTEGER NOT NULL DEFAULT 0",
            ("jobs", "sites"): "TEXT NOT NULL DEFAULT '[]'",
            ("jobs", "preferred_sites"): "TEXT NOT NULL DEFAULT '[]'",
            ("articles", "preferred"): "INTEGER NOT NULL DEFAULT 0",
            ("articles", "sort_order"): "INTEGER NOT NULL DEFAULT 0",
            ("articles", "manual"): "INTEGER NOT NULL DEFAULT 0",
        }
        for (table, column), definition in additions.items():
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _ensure_section_index(conn: sqlite3.Connection) -> None:
        """한 날짜에 같은 갈래를 두 번 수집하지 않도록 막는다.

        갈래 구분이 없던 시절의 데이터가 남아 있으면 유일 인덱스를 만들 수 없다.
        그때는 조회용 인덱스만 두고, 중복 등록은 API 단계에서 거른다.
        """
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_date_section "
                "ON jobs(report_date, section)"
            )
        except sqlite3.IntegrityError:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_date_section_nonunique "
                "ON jobs(report_date, section)"
            )

    def initialize(self) -> None:
        with self.connection() as conn:
            self._retire_legacy_schema(conn)
            self._add_missing_columns(conn)
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    report_date TEXT NOT NULL,
                    section TEXT NOT NULL DEFAULT 'council',
                    name TEXT NOT NULL,
                    keywords TEXT NOT NULL,
                    exclude_keywords TEXT NOT NULL DEFAULT '[]',
                    sites TEXT NOT NULL DEFAULT '[]',
                    preferred_sites TEXT NOT NULL DEFAULT '[]',
                    match_mode TEXT NOT NULL DEFAULT 'any'
                        CHECK(match_mode IN ('any', 'all')),
                    generate_briefing INTEGER NOT NULL DEFAULT 1,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'running', 'complete', 'failed')),
                    article_count INTEGER NOT NULL DEFAULT 0,
                    unique_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_run_at TEXT,
                    approved_at TEXT,
                    UNIQUE(report_date, name)
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_report_date
                    ON jobs(report_date DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_status
                    ON jobs(status, window_end);

                CREATE TABLE IF NOT EXISTS articles (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    report_date TEXT NOT NULL,
                    title TEXT NOT NULL,
                    publisher TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    scraped_at TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    matched_keywords TEXT NOT NULL DEFAULT '[]',
                    preferred INTEGER NOT NULL DEFAULT 0,
                    excluded INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    manual INTEGER NOT NULL DEFAULT 0,
                    duplicate_of TEXT REFERENCES articles(id),
                    UNIQUE(job_id, source_url)
                );

                CREATE INDEX IF NOT EXISTS idx_articles_job
                    ON articles(job_id, published_at DESC);
                CREATE INDEX IF NOT EXISTS idx_articles_duplicate_of
                    ON articles(duplicate_of);

                CREATE TABLE IF NOT EXISTS article_images (
                    id TEXT PRIMARY KEY,
                    article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                    source_url TEXT NOT NULL,
                    caption TEXT NOT NULL DEFAULT '',
                    path TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    byte_size INTEGER NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_article_images_article
                    ON article_images(article_id, position);

                CREATE TABLE IF NOT EXISTS briefings (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model TEXT NOT NULL,
                    generated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_section_index(conn)

    # -- jobs ------------------------------------------------------------

    def create_job(
        self,
        *,
        report_date: str,
        section: str,
        name: str,
        keywords: list[str],
        exclude_keywords: list[str],
        sites: list[str],
        preferred_sites: list[str],
        match_mode: str,
        generate_briefing: bool,
        window_start: str,
        window_end: str,
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        stamp = _now()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO jobs
                    (id, report_date, section, name, keywords, exclude_keywords, sites,
                     preferred_sites, match_mode, generate_briefing, window_start, window_end,
                     status, article_count, unique_count, error_message,
                     created_at, updated_at, last_run_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 0, NULL, ?, ?, NULL)
                """,
                (
                    job_id,
                    report_date,
                    section,
                    name,
                    json.dumps(keywords, ensure_ascii=False),
                    json.dumps(exclude_keywords, ensure_ascii=False),
                    json.dumps(sites, ensure_ascii=False),
                    json.dumps(preferred_sites, ensure_ascii=False),
                    match_mode,
                    1 if generate_briefing else 0,
                    window_start,
                    window_end,
                    stamp,
                    stamp,
                ),
            )
        job = self.get_job(job_id)
        assert job is not None
        return job

    @staticmethod
    def _job_row(row: sqlite3.Row) -> dict[str, Any]:
        job = dict(row)
        job["keywords"] = json.loads(job["keywords"])
        job["exclude_keywords"] = json.loads(job["exclude_keywords"])
        job["sites"] = json.loads(job["sites"] or "[]")
        job["preferred_sites"] = json.loads(job["preferred_sites"] or "[]")
        job["generate_briefing"] = bool(job["generate_briefing"])
        return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_row(row) if row else None

    def jobs(
        self,
        *,
        report_date: str | None = None,
        status: str | None = None,
        section: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if report_date:
            clauses.append("report_date=?")
            params.append(report_date)
        if status:
            clauses.append("status=?")
            params.append(status)
        if section:
            clauses.append("section=?")
            params.append(section)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY report_date DESC, created_at", params
            ).fetchall()
        return [self._job_row(row) for row in rows]

    def due_jobs(self, now_iso: str) -> list[dict[str, Any]]:
        """Pending jobs whose collection window has already closed."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status='pending' AND window_end <= ?
                ORDER BY report_date, created_at
                """,
                (now_iso,),
            ).fetchall()
        return [self._job_row(row) for row in rows]

    def reset_interrupted_jobs(self) -> int:
        """서버가 수집 도중 종료되면 'running'으로 남는다. 다시 대기 상태로 돌린다."""
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs SET
                    status='pending',
                    error_message='이전 실행이 서버 종료로 중단되었습니다.',
                    updated_at=?
                WHERE status='running'
                """,
                (_now(),),
            )
        return cursor.rowcount

    def job_for_section(self, report_date: str, section: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE report_date=? AND section=?", (report_date, section)
            ).fetchone()
        return self._job_row(row) if row else None

    def delete_job(self, job_id: str) -> bool:
        with self.connection() as conn:
            cursor = conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return cursor.rowcount > 0

    def update_job_run(
        self,
        job_id: str,
        *,
        status: str,
        article_count: int | None = None,
        unique_count: int | None = None,
        error_message: str | None = None,
        last_run_at: str | None = None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE jobs SET
                    status=?,
                    article_count=COALESCE(?, article_count),
                    unique_count=COALESCE(?, unique_count),
                    error_message=?,
                    last_run_at=COALESCE(?, last_run_at),
                    updated_at=?
                WHERE id=?
                """,
                (status, article_count, unique_count, error_message, last_run_at, _now(), job_id),
            )

    def dates(self) -> list[str]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT report_date FROM jobs ORDER BY report_date DESC"
            ).fetchall()
        return [row["report_date"] for row in rows]

    # -- articles --------------------------------------------------------

    def upsert_article(self, article: dict[str, Any]) -> None:
        fields = (
            "id", "job_id", "report_date", "title", "publisher", "source_url",
            "published_at", "scraped_at", "summary", "content", "content_hash",
            "matched_keywords", "preferred", "manual", "duplicate_of",
        )
        # 선택 항목은 빠져 있어도 기본값으로 채운다.
        payload = {"matched_keywords": [], "preferred": 0, "manual": 0, "duplicate_of": None}
        payload.update(article)
        matched = payload.get("matched_keywords", [])
        if not isinstance(matched, str):
            payload["matched_keywords"] = json.dumps(matched, ensure_ascii=False)
        values = tuple(payload[field] for field in fields)
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        with self.connection() as conn:
            conn.execute(
                f"""
                INSERT INTO articles ({columns}) VALUES ({placeholders})
                ON CONFLICT(job_id, source_url) DO UPDATE SET
                    title=excluded.title,
                    publisher=excluded.publisher,
                    published_at=excluded.published_at,
                    scraped_at=excluded.scraped_at,
                    summary=excluded.summary,
                    content=excluded.content,
                    content_hash=excluded.content_hash,
                    matched_keywords=excluded.matched_keywords,
                    preferred=excluded.preferred,
                    manual=MAX(articles.manual, excluded.manual)
                """,
                values,
            )

    def articles(
        self,
        job_id: str,
        *,
        unique_only: bool = False,
        include_excluded: bool = True,
    ) -> list[dict[str, Any]]:
        clause = "AND duplicate_of IS NULL" if unique_only else ""
        if not include_excluded:
            clause += " AND excluded = 0"
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT id, job_id, report_date, title, publisher, source_url, published_at,
                       scraped_at, summary, content, content_hash, matched_keywords,
                       preferred, excluded, sort_order, manual, duplicate_of
                FROM articles
                WHERE job_id=? {clause}
                -- 관리자가 정한 순서가 먼저다. 정하지 않았으면(0) 지역 매체가 위로 온다.
                ORDER BY
                    CASE WHEN sort_order > 0 THEN 0 ELSE 1 END,
                    sort_order,
                    preferred DESC,
                    published_at DESC,
                    publisher COLLATE NOCASE,
                    title
                """,
                (job_id,),
            ).fetchall()
        images = self.images_by_article(job_id)
        articles: list[dict[str, Any]] = []
        for row in rows:
            article = dict(row)
            article["matched_keywords"] = json.loads(article["matched_keywords"] or "[]")
            article["preferred"] = bool(article["preferred"])
            article["manual"] = bool(article["manual"])
            article["excluded"] = bool(article["excluded"])
            article["images"] = images.get(article["id"], [])
            articles.append(article)
        return articles

    # -- article images --------------------------------------------------

    def replace_article_images(self, article_id: str, images: list[dict[str, Any]]) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM article_images WHERE article_id=?", (article_id,))
            conn.executemany(
                """
                INSERT INTO article_images
                    (id, article_id, source_url, caption, path, width, height, byte_size, position)
                VALUES (:id, :article_id, :source_url, :caption, :path, :width, :height,
                        :byte_size, :position)
                """,
                images,
            )

    def images_by_article(self, job_id: str) -> dict[str, list[dict[str, Any]]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT image.id, image.article_id, image.source_url, image.caption,
                       image.path, image.width, image.height, image.byte_size, image.position
                FROM article_images AS image
                JOIN articles ON articles.id = image.article_id
                WHERE articles.job_id = ?
                ORDER BY image.article_id, image.position
                """,
                (job_id,),
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["article_id"], []).append(dict(row))
        return grouped

    def get_image(self, image_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT image.id, image.article_id, image.path, image.source_url,
                       articles.job_id
                FROM article_images AS image
                JOIN articles ON articles.id = image.article_id
                WHERE image.id = ?
                """,
                (image_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_excluded_articles(self, job_id: str, excluded_ids: list[str]) -> int:
        """관리자가 고른 기사만 제외로 표시한다. 나머지는 모두 포함으로 되돌린다."""
        with self.connection() as conn:
            conn.execute("UPDATE articles SET excluded=0 WHERE job_id=?", (job_id,))
            if excluded_ids:
                placeholders = ", ".join("?" for _ in excluded_ids)
                conn.execute(
                    f"UPDATE articles SET excluded=1 WHERE job_id=? AND id IN ({placeholders})",
                    (job_id, *excluded_ids),
                )
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM articles WHERE job_id=? AND excluded=1", (job_id,)
            ).fetchone()
        return row["total"]

    def set_article_order(self, job_id: str, ordered_ids: list[str]) -> int:
        """관리자가 정한 순서를 1부터 매긴다. 목록에 없는 기사는 0으로 되돌린다."""
        with self.connection() as conn:
            conn.execute("UPDATE articles SET sort_order=0 WHERE job_id=?", (job_id,))
            conn.executemany(
                "UPDATE articles SET sort_order=? WHERE job_id=? AND id=?",
                [(index, job_id, article_id) for index, article_id in enumerate(ordered_ids, 1)],
            )
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM articles WHERE job_id=? AND sort_order > 0", (job_id,)
            ).fetchone()
        return row["total"]

    def set_approved(self, job_id: str, approved: bool) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE jobs SET approved_at=?, updated_at=? WHERE id=?",
                (_now() if approved else None, _now(), job_id),
            )

    def set_duplicate(self, article_id: str, duplicate_of: str | None) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE articles SET duplicate_of=? WHERE id=?", (duplicate_of, article_id))

    # -- briefings -------------------------------------------------------

    def get_briefing(self, job_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT job_id, body, status, model, generated_at FROM briefings WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_briefing(
        self, job_id: str, body: str, status: str, model: str, generated_at: str
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO briefings (job_id, body, status, model, generated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    body=excluded.body, status=excluded.status, model=excluded.model,
                    generated_at=excluded.generated_at
                """,
                (job_id, body, status, model, generated_at),
            )
