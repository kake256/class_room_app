"""Classroom API から提出物を取得し、PDFに統一して保存する。

注意: Classroom API では成績を書き込めるのは課題を作成したAPIプロジェクトのみの
ため、成績の書き戻しは実装しない(CSV/シート出力まで)。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import pathlib
import subprocess
from typing import Any

from .config import Config

log = logging.getLogger(__name__)

SCOPES = [
    # 読み書き両用(draftGradeの書き込みに必要。readonlyを包含する)
    "https://www.googleapis.com/auth/classroom.coursework.students",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

GDOC_MIME = "application/vnd.google-apps.document"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"


def normalize_gid(gid: str) -> str:
    """ClassroomのURLに出るBase64形式のID(例: MTIzNDU2Nzg5MDEy)を
    APIが要求する数値IDにデコードする。数値ならそのまま返す。"""
    gid = gid.strip()
    if gid.isdigit():
        return gid
    try:
        decoded = base64.b64decode(gid + "=" * (-len(gid) % 4)).decode()
        if decoded.isdigit():
            return decoded
    except Exception:
        pass
    return gid


def _course_id(cfg: Config) -> str:
    return normalize_gid(str(cfg.get("classroom", "course_id", default="")))


def list_courseworks(cfg: Config) -> list[dict[str, Any]]:
    """コースの課題一覧(id, タイトル, 締切, 配点)を表示して返す。過去課題のID確認用。"""
    classroom, _ = get_services(cfg)
    course_id = _course_id(cfg)
    items: list[dict[str, Any]] = []
    page_token = None
    while True:
        resp = (
            classroom.courses()
            .courseWork()
            .list(courseId=course_id, orderBy="dueDate desc", pageToken=page_token)
            .execute()
        )
        items += resp.get("courseWork", [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    print(f"{'courseWorkId':<16} {'締切':<12} {'配点':<4} タイトル")
    for w in items:
        due = w.get("dueDate", {})
        due_s = f"{due.get('year', '----')}-{due.get('month', '--'):>02}-{due.get('day', '--'):>02}" if due else "-"
        print(f"{w['id']:<16} {due_s:<12} {str(w.get('maxPoints', '-')):<4} {w.get('title', '')}")
    return items


def fetch_assigned_grades(cfg: Config, coursework_id: str) -> dict[str, Any]:
    """過去課題の確定済み成績(assignedGrade)を studentId → 点数 で返す。

    過去課題での傾向検証(verify)の正解データとして使う。
    """
    classroom, _ = get_services(cfg)
    course_id = _course_id(cfg)
    grades: dict[str, Any] = {}
    page_token = None
    while True:
        resp = (
            classroom.courses()
            .courseWork()
            .studentSubmissions()
            .list(courseId=course_id, courseWorkId=coursework_id, pageToken=page_token)
            .execute()
        )
        for sub in resp.get("studentSubmissions", []):
            if "assignedGrade" in sub:
                grades[sub["userId"]] = sub["assignedGrade"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return grades


def get_roster(classroom, course_id: str) -> dict[str, dict[str, str]]:
    """userId → {name, email} の対応表を名簿APIから取得する。"""
    roster: dict[str, dict[str, str]] = {}
    page_token = None
    while True:
        resp = (
            classroom.courses()
            .students()
            .list(courseId=course_id, pageToken=page_token)
            .execute()
        )
        for s in resp.get("students", []):
            prof = s.get("profile", {})
            roster[s["userId"]] = {
                "name": prof.get("name", {}).get("fullName", ""),
                "email": prof.get("emailAddress", ""),
            }
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return roster


def get_services(cfg: Config):
    """OAuth(TAアカウント)で classroom / drive サービスを得る。"""
    # Googleは coursework.students.readonly の別名として
    # student-submissions.students.readonly を返すことがあり、
    # oauthlib の厳格なスコープ一致検証に引っかかるため緩和する
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_file = cfg.get("classroom", "token_file", default="token.json")
    creds = None
    token_path = pathlib.Path(token_file)
    # 空ファイル(マウント用プレースホルダ)や壊れたトークンは未認証として扱う
    if token_path.exists() and token_path.stat().st_size > 0:
        try:
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        except ValueError as e:
            log.warning("token.json を読めないため再認証します: %s", e)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            cred_file = cfg.get("classroom", "credentials_file", default="credentials.json")
            cred_path = pathlib.Path(cred_file)
            if not cred_path.exists() or cred_path.stat().st_size == 0:
                raise SystemExit(
                    f"{cred_file} がありません(または空です)。\n"
                    "Google Cloud Console で OAuth クライアントID(デスクトップアプリ)を作成し、\n"
                    "ダウンロードしたJSONをプロジェクト直下に credentials.json として配置してください。\n"
                    "手順: https://console.cloud.google.com/apis/credentials\n"
                    "  1. プロジェクト作成 → Classroom API と Drive API を有効化\n"
                    "  2. OAuth同意画面を設定(テストユーザーにTAアカウントを追加)\n"
                    "  3. 認証情報 → OAuthクライアントID(デスクトップアプリ)→ JSONダウンロード"
                )
            flow = InstalledAppFlow.from_client_secrets_file(cred_file, SCOPES)
            # Docker内ではブラウザを開けないため、URLを表示して手動で開いてもらう
            # (docker-compose で 8765 ポートをホストに公開している)
            creds = flow.run_local_server(
                port=8765, bind_addr="0.0.0.0", open_browser=False,
            )
        pathlib.Path(token_file).write_text(creds.to_json())
    return build("classroom", "v1", credentials=creds), build("drive", "v3", credentials=creds)


def convert_docx_to_pdf(docx: pathlib.Path, out_dir: pathlib.Path) -> pathlib.Path:
    subprocess.run(
        ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(docx)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    pdf = out_dir / (docx.stem + ".pdf")
    if not pdf.exists():
        raise RuntimeError(f"LibreOffice conversion produced no output for {docx.name}")
    return pdf


def _download(drive, file_id: str, dest: pathlib.Path, export_pdf: bool = False) -> None:
    from googleapiclient.http import MediaIoBaseDownload

    req = (
        drive.files().export_media(fileId=file_id, mimeType=PDF_MIME)
        if export_pdf
        else drive.files().get_media(fileId=file_id)
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        dl = MediaIoBaseDownload(f, req)
        done = False
        while not done:
            _, done = dl.next_chunk()


def fetch_submissions(cfg: Config, coursework_id: str) -> list[dict[str, Any]]:
    """提出物をダウンロードしてPDFに統一、メタデータを meta/<cw>.jsonl に保存。

    冪等: DriveのmodifiedTimeが前回と同じならダウンロードをスキップ。
    再提出(modifiedTime変化)は上書き取得し、下流で自動再採点される。
    ローカル運用のため匿名化は行わず、userId と実名をそのまま扱う。
    """
    classroom, drive = get_services(cfg)
    course_id = _course_id(cfg)
    data = cfg.data_dir
    raw_dir = data / "raw" / coursework_id
    pdf_dir = data / "pdf" / coursework_id
    meta_path = data / "meta" / f"{coursework_id}.jsonl"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)

    prev_meta: dict[str, dict[str, Any]] = {}
    if meta_path.exists():
        for line in meta_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                m = json.loads(line)
                prev_meta[m["student_id"]] = m

    roster = get_roster(classroom, course_id)

    metas: list[dict[str, Any]] = []
    page_token = None
    while True:
        resp = (
            classroom.courses()
            .courseWork()
            .studentSubmissions()
            .list(courseId=course_id, courseWorkId=coursework_id, pageToken=page_token)
            .execute()
        )
        for sub in resp.get("studentSubmissions", []):
            sid = sub["userId"]
            student = roster.get(sid, {})
            meta: dict[str, Any] = {
                "student_id": sid,
                "name": student.get("name", ""),
                "email": student.get("email", ""),
                "state": sub.get("state"),
                "late": bool(sub.get("late", False)),
                "update_time": sub.get("updateTime"),
                "format_violation": False,
            }
            attachments = sub.get("assignmentSubmission", {}).get("attachments", [])
            drive_files = [a["driveFile"] for a in attachments if "driveFile" in a]
            # RETURNED = 提出済みで採点返却済み(過去課題の検証で使う)
            if sub.get("state") in ("TURNED_IN", "RETURNED") and drive_files:
                df_meta = (
                    drive.files()
                    .get(fileId=drive_files[0]["id"], fields="mimeType,name,modifiedTime")
                    .execute()
                )
                meta.update(
                    file_name=df_meta["name"],
                    mime_type=df_meta["mimeType"],
                    modified_time=df_meta["modifiedTime"],
                )
                prev = prev_meta.get(sid, {})
                if prev.get("modified_time") == df_meta["modifiedTime"] and (
                    pdf_dir / f"{sid}.pdf"
                ).exists():
                    meta["format_violation"] = prev.get("format_violation", False)
                    log.info("%s: unchanged, skip download", sid)
                else:
                    try:
                        _fetch_one(drive, drive_files[0]["id"], df_meta, sid, raw_dir, pdf_dir, meta)
                    except Exception as e:
                        log.error("%s: fetch failed: %s", sid, e)
                        meta["error"] = str(e)
            metas.append(meta)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    # 未提出者も名簿から meta に載せる(report の「未提出 x人」用)
    seen = {m["student_id"] for m in metas}
    for uid, student in roster.items():
        if uid not in seen:
            metas.append(
                {
                    "student_id": uid,
                    "name": student.get("name", ""),
                    "email": student.get("email", ""),
                    "state": "NOT_SUBMITTED",
                    "late": False,
                    "format_violation": False,
                }
            )

    with open(meta_path, "w", encoding="utf-8") as f:
        for m in metas:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    return metas


def _fetch_one(drive, file_id, df_meta, sid, raw_dir, pdf_dir, meta) -> None:
    mime = df_meta["mimeType"]
    target_pdf = pdf_dir / f"{sid}.pdf"
    if mime == GDOC_MIME:
        _download(drive, file_id, target_pdf, export_pdf=True)
    elif mime == PDF_MIME:
        _download(drive, file_id, target_pdf)
    elif mime == DOCX_MIME:
        raw = raw_dir / sid / df_meta["name"]
        _download(drive, file_id, raw)
        convert_docx_to_pdf(raw, pdf_dir).rename(target_pdf)
    else:
        # 形式違反(画像単体等): 採点スキップ、0点仮置き+レビュー行き
        raw = raw_dir / sid / df_meta["name"]
        _download(drive, file_id, raw)
        meta["format_violation"] = True
        log.warning("%s: unsupported mime %s (format violation)", sid, mime)
