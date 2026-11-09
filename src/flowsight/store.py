import sqlite3
import threading
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from flowsight.domain import PullRequest, Review, ReviewState, from_iso, to_iso

# Schema versions in order; `PRAGMA user_version` records how many have been applied.
_MIGRATIONS = (
    """
    CREATE TABLE repositories (
      key             TEXT PRIMARY KEY,  -- lower-cased owner/name
      name_with_owner TEXT NOT NULL,
      covered_from    TEXT,              -- both set when a sync completes
      synced_at       TEXT
    ) STRICT;

    CREATE TABLE pull_requests (
      repo               TEXT NOT NULL REFERENCES repositories (key) ON DELETE CASCADE,
      number             INTEGER NOT NULL,
      title              TEXT NOT NULL,
      author             TEXT,
      author_is_bot      INTEGER NOT NULL,
      author_association TEXT NOT NULL,
      created_at         TEXT NOT NULL,
      ready_at           TEXT,
      merged_at          TEXT,
      closed_at          TEXT,
      updated_at         TEXT NOT NULL,
      additions          INTEGER NOT NULL,
      deletions          INTEGER NOT NULL,
      PRIMARY KEY (repo, number)
    ) STRICT;

    CREATE INDEX pull_requests_by_created ON pull_requests (repo, created_at);
    CREATE INDEX pull_requests_by_closed ON pull_requests (repo, closed_at);

    CREATE TABLE reviews (
      repo            TEXT NOT NULL,
      pr_number       INTEGER NOT NULL,
      reviewer        TEXT,
      reviewer_is_bot INTEGER NOT NULL,
      state           TEXT NOT NULL,
      submitted_at    TEXT NOT NULL,
      FOREIGN KEY (repo, pr_number) REFERENCES pull_requests (repo, number) ON DELETE CASCADE
    ) STRICT;

    CREATE INDEX reviews_by_pull_request ON reviews (repo, pr_number);
    """,
    """
    CREATE TABLE insights (
      key            TEXT PRIMARY KEY,  -- hash of prompt version, model and evidence
      model          TEXT NOT NULL,
      prompt_version TEXT NOT NULL,
      body           TEXT NOT NULL,     -- the validated LLM answer, as JSON
      created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    ) STRICT;
    """,
)


def open_database(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # One connection shared by the event loop and worker threads; PullRequestStore serialises
    # access with a lock. Queries here take milliseconds.
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    _migrate(connection)
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    applied = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])
    for version, sql in enumerate(_MIGRATIONS[applied:], start=applied + 1):
        with connection:
            for statement in filter(str.strip, sql.split(";")):
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {version}")


@dataclass(frozen=True, slots=True)
class SyncState:
    covered_from: datetime  # every PR opened or closed at or after this instant is stored
    synced_at: datetime  # when the last complete sync started


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else to_iso(moment)


def _datetime(value: str | None) -> datetime | None:
    return None if value is None else from_iso(value)


class PullRequestStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection
        self._lock = threading.Lock()

    def cached_insight(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT body FROM insights WHERE key = ?", (key,)).fetchone()
        return None if row is None else cast(str, row["body"])

    def save_insight(self, key: str, model: str, prompt_version: str, body: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO insights (key, model, prompt_version, body) VALUES (?, ?, ?, ?)
                   ON CONFLICT (key) DO NOTHING""",
                (key, model, prompt_version, body),
            )

    def ensure_repository(self, repo: str, name_with_owner: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO repositories (key, name_with_owner) VALUES (?, ?)
                   ON CONFLICT (key) DO UPDATE SET name_with_owner = excluded.name_with_owner""",
                (repo, name_with_owner),
            )

    def name_with_owner(self, repo: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT name_with_owner FROM repositories WHERE key = ?", (repo,)
            ).fetchone()
        return None if row is None else cast(str, row["name_with_owner"])

    def sync_state(self, repo: str) -> SyncState | None:
        with self._lock:
            row = self._db.execute(
                "SELECT covered_from, synced_at FROM repositories WHERE key = ?", (repo,)
            ).fetchone()
        if row is None or row["covered_from"] is None or row["synced_at"] is None:
            return None
        return SyncState(from_iso(row["covered_from"]), from_iso(row["synced_at"]))

    def save_sync_state(self, repo: str, state: SyncState) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE repositories SET covered_from = ?, synced_at = ? WHERE key = ?",
                (to_iso(state.covered_from), to_iso(state.synced_at), repo),
            )

    def updated_at_by_number(self, repo: str, numbers: Sequence[int]) -> dict[int, datetime]:
        """Stored `updated_at` per PR number, so unchanged pull requests are not fetched again."""
        with self._lock:
            rows = self._db.execute(
                """SELECT number, updated_at FROM pull_requests
                   WHERE repo = ? AND number IN (SELECT value FROM json_each(?))""",
                (repo, f"[{','.join(map(str, numbers))}]"),
            ).fetchall()
        return {row["number"]: from_iso(row["updated_at"]) for row in rows}

    def save(self, repo: str, pull_requests: Sequence[PullRequest]) -> None:
        """Inserts or updates pull requests and replaces their reviews, atomically."""
        with self._lock, self._db:
            for pr in pull_requests:
                self._db.execute(
                    """INSERT INTO pull_requests (repo, number, title, author, author_is_bot,
                         author_association, created_at, ready_at, merged_at, closed_at,
                         updated_at, additions, deletions)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (repo, number) DO UPDATE SET
                         title = excluded.title, author = excluded.author,
                         author_is_bot = excluded.author_is_bot,
                         author_association = excluded.author_association,
                         created_at = excluded.created_at, ready_at = excluded.ready_at,
                         merged_at = excluded.merged_at, closed_at = excluded.closed_at,
                         updated_at = excluded.updated_at, additions = excluded.additions,
                         deletions = excluded.deletions""",
                    (
                        repo, pr.number, pr.title, pr.author, int(pr.author_is_bot),
                        pr.author_association, to_iso(pr.created_at), _iso(pr.ready_at),
                        _iso(pr.merged_at), _iso(pr.closed_at), to_iso(pr.updated_at),
                        pr.additions, pr.deletions,
                    ),
                )  # fmt: skip
                self._db.execute(
                    "DELETE FROM reviews WHERE repo = ? AND pr_number = ?", (repo, pr.number)
                )
                self._db.executemany(
                    """INSERT INTO reviews (repo, pr_number, reviewer, reviewer_is_bot, state,
                         submitted_at) VALUES (?, ?, ?, ?, ?, ?)""",
                    [
                        (repo, pr.number, r.reviewer, int(r.reviewer_is_bot), r.state,
                         to_iso(r.submitted_at))
                        for r in pr.reviews
                    ],
                )  # fmt: skip

    def pull_requests_since(self, repo: str, since: datetime) -> list[PullRequest]:
        """Pull requests opened or closed at or after `since`, with reviews in submission order."""
        params = {"repo": repo, "since": to_iso(since)}
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM pull_requests
                   WHERE repo = :repo AND (created_at >= :since OR closed_at >= :since)
                   ORDER BY number""",
                params,
            ).fetchall()
            review_rows = self._db.execute(
                """SELECT pr_number, reviewer, reviewer_is_bot, state, submitted_at
                   FROM reviews
                   WHERE repo = :repo AND pr_number IN (
                     SELECT number FROM pull_requests
                     WHERE repo = :repo AND (created_at >= :since OR closed_at >= :since))
                   ORDER BY submitted_at""",
                params,
            ).fetchall()

        reviews: defaultdict[int, list[Review]] = defaultdict(list)
        for row in review_rows:
            reviews[row["pr_number"]].append(
                Review(
                    reviewer=row["reviewer"],
                    reviewer_is_bot=bool(row["reviewer_is_bot"]),
                    state=cast(ReviewState, row["state"]),
                    submitted_at=from_iso(row["submitted_at"]),
                )
            )
        return [
            PullRequest(
                number=row["number"],
                title=row["title"],
                author=row["author"],
                author_is_bot=bool(row["author_is_bot"]),
                author_association=row["author_association"],
                created_at=from_iso(row["created_at"]),
                ready_at=_datetime(row["ready_at"]),
                merged_at=_datetime(row["merged_at"]),
                closed_at=_datetime(row["closed_at"]),
                updated_at=from_iso(row["updated_at"]),
                additions=row["additions"],
                deletions=row["deletions"],
                reviews=tuple(reviews[row["number"]]),
            )
            for row in rows
        ]
