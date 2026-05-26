from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
import json
import sqlite3
import sys
import time


ROOT_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT_DIR / "public"
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "myregister.sqlite3"


def now_iso():
    """return the current local timestamp in a compact iso format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def default_academic_year_name():
    """return the current academic year label."""
    now = datetime.now()
    start_year = now.year if now.month >= 9 else now.year - 1
    return f"{start_year}/{start_year + 1}"


def mode_scale(mode):
    return 30 if mode == "university" else 10


def normalize_date(value):
    """accept an optional yyyy-mm-dd date from the client."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def connect_db():
    """open a sqlite connection with dictionaries as row results."""
    DATA_DIR.mkdir(exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db():
    """create the local database tables when the app starts."""
    with connect_db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS academic_years (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('school', 'university')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(name, mode)
            );

            CREATE TABLE IF NOT EXISTS subjects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                academic_year_id INTEGER,
                name TEXT NOT NULL,
                scale REAL NOT NULL DEFAULT 10,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (academic_year_id) REFERENCES academic_years(id) ON DELETE CASCADE,
                UNIQUE(academic_year_id, name)
            );

            CREATE TABLE IF NOT EXISTS grades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id INTEGER NOT NULL,
                value REAL NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                weight REAL NOT NULL DEFAULT 100,
                grade_date TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
            );
            """
        )
        grade_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(grades)")
        }
        if "grade_date" not in grade_columns:
            connection.execute("ALTER TABLE grades ADD COLUMN grade_date TEXT")
        migrate_academic_years(connection)


def ensure_academic_year(connection, name, mode):
    timestamp = now_iso()
    row = connection.execute(
        "SELECT id FROM academic_years WHERE name = ? AND mode = ?",
        (name, mode),
    ).fetchone()
    if row is not None:
        return row["id"]

    cursor = connection.execute(
        """
        INSERT INTO academic_years (name, mode, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (name, mode, timestamp, timestamp),
    )
    return cursor.lastrowid


def migrate_academic_years(connection):
    """move older databases into year-scoped subjects."""
    default_name = default_academic_year_name()
    school_year_id = ensure_academic_year(connection, default_name, "school")
    university_year_id = ensure_academic_year(connection, default_name, "university")

    subject_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(subjects)")
    }
    subject_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'subjects'"
    ).fetchone()["sql"]

    if "academic_year_id" not in subject_columns:
        connection.execute("ALTER TABLE subjects ADD COLUMN academic_year_id INTEGER")
        subject_columns.add("academic_year_id")

    connection.execute(
        """
        UPDATE subjects
        SET academic_year_id = CASE
            WHEN scale = 30 THEN ?
            ELSE ?
        END
        WHERE academic_year_id IS NULL
        """,
        (university_year_id, school_year_id),
    )

    if "name TEXT NOT NULL UNIQUE" not in subject_sql:
        return

    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        """
        CREATE TABLE subjects_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            academic_year_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            scale REAL NOT NULL DEFAULT 10,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (academic_year_id) REFERENCES academic_years(id) ON DELETE CASCADE,
            UNIQUE(academic_year_id, name)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO subjects_new (id, academic_year_id, name, scale, created_at, updated_at)
        SELECT id, academic_year_id, name, scale, created_at, updated_at
        FROM subjects
        """
    )
    connection.execute("DROP TABLE subjects")
    connection.execute("ALTER TABLE subjects_new RENAME TO subjects")
    connection.execute("PRAGMA foreign_keys = ON")


def to_float(value, fallback=None):
    """convert client input to float while keeping empty values predictable."""
    if value is None or value == "":
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def serialize_subjects():
    """load subjects and grades with calculated weighted averages."""
    with connect_db() as connection:
        academic_year_rows = connection.execute(
            """
            SELECT *
            FROM academic_years
            ORDER BY name DESC, CASE mode WHEN 'school' THEN 0 ELSE 1 END, id DESC
            """
        ).fetchall()
        subject_rows = connection.execute(
            "SELECT * FROM subjects ORDER BY name COLLATE NOCASE"
        ).fetchall()
        grade_rows = connection.execute(
            """
            SELECT * FROM grades
            ORDER BY COALESCE(grade_date, created_at) DESC, id DESC
            """
        ).fetchall()

    grades_by_subject = {}
    for row in grade_rows:
        grade = dict(row)
        grade["value"] = float(grade["value"])
        grade["weight"] = float(grade["weight"])
        grades_by_subject.setdefault(grade["subject_id"], []).append(grade)

    subjects = []
    all_weighted_normalized = 0
    all_weight = 0

    for row in subject_rows:
        subject = dict(row)
        subject["scale"] = float(subject["scale"])
        grades = grades_by_subject.get(subject["id"], [])
        weighted_total = 0
        weight_total = 0

        for grade in grades:
            weight = max(float(grade["weight"]), 0)
            weighted_total += float(grade["value"]) * weight
            weight_total += weight
            all_weighted_normalized += (
                (float(grade["value"]) / subject["scale"]) * 100 * weight
            )
            all_weight += weight

        average = weighted_total / weight_total if weight_total else None
        normalized_average = (
            (average / subject["scale"]) * 100 if average is not None else None
        )
        latest_grade_update = max(
            [grade["updated_at"] for grade in grades], default=subject["updated_at"]
        )

        subject.update(
            {
                "grades": grades,
                "average": average,
                "normalizedAverage": normalized_average,
                "lastModified": max(subject["updated_at"], latest_grade_update),
            }
        )
        subjects.append(subject)

    return {
        "academicYears": [dict(row) for row in academic_year_rows],
        "subjects": subjects,
        "totalAverage": (
            (all_weighted_normalized / all_weight) / 10 if all_weight else None
        ),
        "totalAverageScale": 10,
        "totalAveragePercent": (
            all_weighted_normalized / all_weight if all_weight else None
        ),
        "totalGrades": len(grade_rows),
    }


class AppHandler(SimpleHTTPRequestHandler):
    """serve the static app and a small json api."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC_DIR), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/subjects":
            self.send_json(serialize_subjects())
            return

        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if parsed.path == "/api/academic-years":
            self.create_academic_year()
            return
        if parsed.path == "/api/subjects":
            self.create_subject()
            return
        if parsed.path == "/api/grades":
            self.create_grade()
            return
        if len(parts) == 5 and parts[:2] == ["api", "subjects"] and parts[3:] == ["grades", "migrate"]:
            self.migrate_grades(int(parts[2]))
            return
        self.send_error(404)

    def do_PUT(self):
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "grades"]:
            self.update_grade(int(parts[2]))
            return
        if len(parts) == 3 and parts[:2] == ["api", "subjects"]:
            self.update_subject(int(parts[2]))
            return
        self.send_error(404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "grades"]:
            self.delete_grade(int(parts[2]))
            return
        if len(parts) == 4 and parts[:2] == ["api", "subjects"] and parts[3] == "grades":
            self.delete_subject_grades(int(parts[2]))
            return
        if len(parts) == 3 and parts[:2] == ["api", "subjects"]:
            self.delete_subject(int(parts[2]))
            return
        self.send_error(404)

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            return json.loads(raw_body)
        except json.JSONDecodeError:
            self.send_error(400, "invalid json")
            return None

    def send_json(self, payload, status=200):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def create_academic_year(self):
        payload = self.read_json()
        if payload is None:
            return

        name = str(payload.get("name", "")).strip()
        mode = str(payload.get("mode", "")).strip()
        if not name:
            self.send_error(400, "academic year name is required")
            return
        if mode not in {"school", "university"}:
            self.send_error(400, "academic year mode must be school or university")
            return

        try:
            with connect_db() as connection:
                existing = connection.execute(
                    "SELECT id FROM academic_years WHERE name = ? AND mode = ?",
                    (name, mode),
                ).fetchone()
                if existing is not None:
                    self.send_error(409, "academic year already exists")
                    return
                ensure_academic_year(connection, name, mode)
        except sqlite3.IntegrityError:
            self.send_error(409, "academic year already exists")
            return

        self.send_json(serialize_subjects(), 201)

    def create_subject(self):
        payload = self.read_json()
        if payload is None:
            return

        name = str(payload.get("name", "")).strip()
        academic_year_id = int(payload.get("academicYearId") or 0)
        if not name:
            self.send_error(400, "subject name is required")
            return

        timestamp = now_iso()
        try:
            with connect_db() as connection:
                academic_year = connection.execute(
                    "SELECT mode FROM academic_years WHERE id = ?",
                    (academic_year_id,),
                ).fetchone()
                if academic_year is None:
                    self.send_error(404, "academic year not found")
                    return
                scale = mode_scale(academic_year["mode"])
                connection.execute(
                    """
                    INSERT INTO subjects (academic_year_id, name, scale, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (academic_year_id, name, scale, timestamp, timestamp),
                )
        except sqlite3.IntegrityError:
            self.send_error(409, "subject already exists")
            return

        self.send_json(serialize_subjects(), 201)

    def update_subject(self, subject_id):
        payload = self.read_json()
        if payload is None:
            return

        name = str(payload.get("name", "")).strip()
        if not name:
            self.send_error(400, "subject name is required")
            return

        try:
            with connect_db() as connection:
                subject = connection.execute(
                    """
                    SELECT academic_years.mode
                    FROM subjects
                    JOIN academic_years ON academic_years.id = subjects.academic_year_id
                    WHERE subjects.id = ?
                    """,
                    (subject_id,),
                ).fetchone()
                if subject is None:
                    self.send_error(404, "subject not found")
                    return
                scale = mode_scale(subject["mode"])
                result = connection.execute(
                    """
                    UPDATE subjects
                    SET name = ?, scale = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (name, scale, now_iso(), subject_id),
                )
        except sqlite3.IntegrityError:
            self.send_error(409, "subject already exists")
            return

        self.send_json(serialize_subjects())

    def create_grade(self):
        payload = self.read_json()
        if payload is None:
            return

        subject_id = int(payload.get("subjectId") or 0)
        value = to_float(payload.get("value"))
        weight = to_float(payload.get("weight"), 100)
        description = str(payload.get("description", "")).strip()
        grade_date = normalize_date(payload.get("gradeDate"))
        if payload.get("gradeDate") and grade_date is None:
            self.send_error(400, "grade date must use yyyy-mm-dd")
            return

        error = self.validate_grade(subject_id, value, weight)
        if error:
            self.send_error(400, error)
            return

        timestamp = now_iso()
        with connect_db() as connection:
            connection.execute(
                """
                INSERT INTO grades
                    (subject_id, value, description, weight, grade_date, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (subject_id, value, description, weight, grade_date, timestamp, timestamp),
            )
            connection.execute(
                "UPDATE subjects SET updated_at = ? WHERE id = ?",
                (timestamp, subject_id),
            )

        self.send_json(serialize_subjects(), 201)

    def update_grade(self, grade_id):
        payload = self.read_json()
        if payload is None:
            return

        subject_id = int(payload.get("subjectId") or 0)
        value = to_float(payload.get("value"))
        weight = to_float(payload.get("weight"), 100)
        description = str(payload.get("description", "")).strip()
        grade_date = normalize_date(payload.get("gradeDate"))
        if payload.get("gradeDate") and grade_date is None:
            self.send_error(400, "grade date must use yyyy-mm-dd")
            return

        error = self.validate_grade(subject_id, value, weight)
        if error:
            self.send_error(400, error)
            return

        timestamp = now_iso()
        with connect_db() as connection:
            result = connection.execute(
                """
                UPDATE grades
                SET subject_id = ?, value = ?, description = ?, weight = ?, grade_date = ?, updated_at = ?
                WHERE id = ?
                """,
                (subject_id, value, description, weight, grade_date, timestamp, grade_id),
            )
            connection.execute(
                "UPDATE subjects SET updated_at = ? WHERE id = ?",
                (timestamp, subject_id),
            )

        if result.rowcount == 0:
            self.send_error(404, "grade not found")
            return
        self.send_json(serialize_subjects())

    def delete_subject(self, subject_id):
        with connect_db() as connection:
            result = connection.execute("DELETE FROM subjects WHERE id = ?", (subject_id,))
        if result.rowcount == 0:
            self.send_error(404, "subject not found")
            return
        self.send_json(serialize_subjects())

    def delete_grade(self, grade_id):
        timestamp = now_iso()
        with connect_db() as connection:
            row = connection.execute(
                "SELECT subject_id FROM grades WHERE id = ?", (grade_id,)
            ).fetchone()
            if row is None:
                self.send_error(404, "grade not found")
                return
            connection.execute("DELETE FROM grades WHERE id = ?", (grade_id,))
            connection.execute(
                "UPDATE subjects SET updated_at = ? WHERE id = ?",
                (timestamp, row["subject_id"]),
            )
        self.send_json(serialize_subjects())

    def delete_subject_grades(self, subject_id):
        timestamp = now_iso()
        with connect_db() as connection:
            subject = connection.execute(
                "SELECT id FROM subjects WHERE id = ?", (subject_id,)
            ).fetchone()
            if subject is None:
                self.send_error(404, "subject not found")
                return

            connection.execute("DELETE FROM grades WHERE subject_id = ?", (subject_id,))
            connection.execute(
                "UPDATE subjects SET updated_at = ? WHERE id = ?",
                (timestamp, subject_id),
            )
        self.send_json(serialize_subjects())

    def migrate_grades(self, source_subject_id):
        payload = self.read_json()
        if payload is None:
            return

        target_subject_id = int(payload.get("targetSubjectId") or 0)
        grade_ids = payload.get("gradeIds") or []
        if target_subject_id == source_subject_id:
            self.send_error(400, "target subject must be different")
            return
        if not isinstance(grade_ids, list) or not grade_ids:
            self.send_error(400, "at least one grade is required")
            return

        try:
            grade_ids = [int(grade_id) for grade_id in grade_ids]
        except (TypeError, ValueError):
            self.send_error(400, "grade ids must be numeric")
            return

        placeholders = ",".join("?" for _ in grade_ids)
        timestamp = now_iso()
        with connect_db() as connection:
            source = connection.execute(
                "SELECT id, academic_year_id, scale FROM subjects WHERE id = ?", (source_subject_id,)
            ).fetchone()
            target = connection.execute(
                "SELECT id, academic_year_id, scale FROM subjects WHERE id = ?", (target_subject_id,)
            ).fetchone()
            if source is None or target is None:
                self.send_error(404, "subject not found")
                return
            if float(source["scale"]) != float(target["scale"]):
                self.send_error(400, "source and target subjects must use the same scale")
                return
            if int(source["academic_year_id"]) != int(target["academic_year_id"]):
                self.send_error(400, "source and target subjects must use the same academic year")
                return

            rows = connection.execute(
                f"""
                SELECT id, value
                FROM grades
                WHERE subject_id = ? AND id IN ({placeholders})
                """,
                [source_subject_id, *grade_ids],
            ).fetchall()
            if len(rows) != len(set(grade_ids)):
                self.send_error(404, "one or more grades were not found")
                return

            target_scale = float(target["scale"])
            if any(float(row["value"]) > target_scale for row in rows):
                self.send_error(400, f"selected grades must be between 0 and {target['scale']}")
                return

            connection.execute(
                f"""
                UPDATE grades
                SET subject_id = ?, updated_at = ?
                WHERE subject_id = ? AND id IN ({placeholders})
                """,
                [target_subject_id, timestamp, source_subject_id, *grade_ids],
            )
            connection.execute(
                "UPDATE subjects SET updated_at = ? WHERE id IN (?, ?)",
                (timestamp, source_subject_id, target_subject_id),
            )
        self.send_json(serialize_subjects())

    def validate_grade(self, subject_id, value, weight):
        with connect_db() as connection:
            subject = connection.execute(
                "SELECT scale FROM subjects WHERE id = ?", (subject_id,)
            ).fetchone()
        if subject is None:
            return "subject not found"
        if value is None or value < 0 or value > float(subject["scale"]):
            return f"grade must be between 0 and {subject['scale']}"
        if weight is None or weight <= 0:
            return "weight must be greater than zero"
        return None


def main():
    """start the local web server."""
    init_db()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), AppHandler)
    print(f"MyRegister is running at http://127.0.0.1:{port}")
    print(f"sqlite database: {DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
