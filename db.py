"""SQLite storage: patients and their documents."""
import re
import sqlite3
from datetime import datetime

_MAP = {
    "ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "أ": "ا", "إ": "ا", "آ": "ا", "ؤ": "و",
    "‌": " ", "ـ": "",
}
_MAP.update({c: str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})
_MAP.update({c: str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")})
_TRANS = str.maketrans(_MAP)
_DIACRITICS = re.compile(r"[ً-ْٰ]")

_con: sqlite3.Connection | None = None


def norm(s: str) -> str:
    """Normalize Persian/Arabic text so search is spelling-tolerant."""
    s = _DIACRITICS.sub("", s.translate(_TRANS))
    return re.sub(r"\s+", " ", s).strip().lower()


def digits(s: str) -> str:
    return re.sub(r"\D", "", s.translate(_TRANS))


def init(path: str) -> None:
    global _con
    _con = sqlite3.connect(path)
    _con.row_factory = sqlite3.Row
    _con.execute("PRAGMA foreign_keys = ON")
    _con.executescript(
        """
        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_norm TEXT NOT NULL,
            phone TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            path TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_docs_patient ON docs(patient_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_patients_name ON patients(name_norm);
        """
    )
    _con.commit()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def add_patient(name: str, phone: str = "") -> int:
    cur = _con.execute(
        "INSERT INTO patients (name, name_norm, phone, created_at) VALUES (?,?,?,?)",
        (name.strip(), norm(name), digits(phone), _now()),
    )
    _con.commit()
    return cur.lastrowid


def get_patient(pid: int):
    return _con.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()


def same_name(name: str):
    return _con.execute(
        "SELECT * FROM patients WHERE name_norm=? ORDER BY id", (norm(name),)
    ).fetchall()


def search(q: str, limit: int = 20):
    """Digits -> file number or phone. Otherwise -> every word must be in the name."""
    d = digits(q)
    if d and not re.search(r"[^\d\s+\-]", q.translate(_TRANS)):
        # short numbers are file numbers only; 4+ digits may also be part of a phone
        phone = f"%{d}%" if len(d) >= 4 else ""
        return _con.execute(
            "SELECT * FROM patients WHERE id=? OR phone LIKE ? ORDER BY id=? DESC, id DESC LIMIT ?",
            (int(d), phone, int(d), limit),
        ).fetchall()
    words = norm(q).split()
    if not words:
        return []
    where = " AND ".join("name_norm LIKE ?" for _ in words)
    return _con.execute(
        f"SELECT * FROM patients WHERE {where} ORDER BY id DESC LIMIT ?",
        (*[f"%{w}%" for w in words], limit),
    ).fetchall()


def update_patient(pid: int, name: str | None = None, phone: str | None = None) -> None:
    if name is not None:
        _con.execute(
            "UPDATE patients SET name=?, name_norm=? WHERE id=?", (name.strip(), norm(name), pid)
        )
    if phone is not None:
        _con.execute("UPDATE patients SET phone=? WHERE id=?", (digits(phone), pid))
    _con.commit()


def delete_patient(pid: int) -> list[str]:
    """Delete patient and their docs. Returns file paths to remove from disk."""
    paths = [r["path"] for r in list_docs(pid)]
    _con.execute("DELETE FROM patients WHERE id=?", (pid,))
    _con.commit()
    return paths


def add_doc(pid: int, kind: str, path: str) -> int:
    cur = _con.execute(
        "INSERT INTO docs (patient_id, kind, path, created_at) VALUES (?,?,?,?)",
        (pid, kind, path, _now()),
    )
    _con.commit()
    return cur.lastrowid


def get_doc(doc_id: int):
    return _con.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()


def list_docs(pid: int):
    return _con.execute(
        "SELECT * FROM docs WHERE patient_id=? ORDER BY created_at, id", (pid,)
    ).fetchall()


def delete_doc(doc_id: int) -> str | None:
    row = get_doc(doc_id)
    if not row:
        return None
    _con.execute("DELETE FROM docs WHERE id=?", (doc_id,))
    _con.commit()
    return row["path"]
