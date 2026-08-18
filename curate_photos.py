#!/usr/bin/env python3
"""
Googleドライブ内の写真フォルダを巡回し、Gemini APIで写真を評価して
「良い写真」だけを各イベントフォルダ内の「厳選」サブフォルダにコピーする。

必要な環境変数:
  GOOGLE_SERVICE_ACCOUNT_JSON : サービスアカウントの認証情報(JSON文字列)
  GEMINI_API_KEY              : Google AI Studio の Gemini APIキー
  ROOT_FOLDER_ID              : 写真が入っている親フォルダのID
  SCORE_THRESHOLD              : 選定スコアの閾値(デフォルト70)
  GEMINI_MODEL                 : 使用するGeminiモデル名(デフォルト gemini-2.5-flash)

状態管理:
  processed_ids.json に処理済みファイルIDを保存し、リポジトリにコミットして
  次回実行時に再利用する(二重処理防止)。
"""

import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

# ---- 設定 ----------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/drive"]
ROOT_FOLDER_ID = os.environ["ROOT_FOLDER_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
SCORE_THRESHOLD = int(os.environ.get("SCORE_THRESHOLD", "70"))
SELECTED_FOLDER_NAME = "厳選"
STATE_PATH = Path(__file__).parent / "processed_ids.json"
IMAGE_MIME_PREFIX = "image/"

GEMINI_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

# 連写ファイル名の例: IMG_20260809_161346082_BURST000_COVER.jpg / BURST001.jpg など
BURST_RE = re.compile(r"^(?P<base>.+?)_BURST\d+(?:_COVER)?\.\w+$", re.IGNORECASE)


# ---- Google Drive 認証 -----------------------------------------------------

def get_drive_service():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


# ---- 状態の読み書き ---------------------------------------------------------

def load_processed_ids() -> set:
    if STATE_PATH.exists():
        return set(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    return set()


def save_processed_ids(ids: set) -> None:
    STATE_PATH.write_text(
        json.dumps(sorted(ids), ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---- Drive操作 -------------------------------------------------------------

def list_subfolders(service, parent_id: str):
    query = (
        f"'{parent_id}' in parents and "
        f"mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    folders, page_token = [], None
    while True:
        resp = service.files().list(
            q=query, fields="nextPageToken, files(id, name)", pageToken=page_token
        ).execute()
        folders.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return folders


def list_images(service, parent_id: str):
    query = (
        f"'{parent_id}' in parents and "
        f"mimeType contains 'image/' and trashed = false"
    )
    files, page_token = [], None
    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType, createdTime)",
            pageToken=page_token,
            orderBy="name",
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return files


def find_or_create_subfolder(service, parent_id: str, name: str) -> str:
    query = (
        f"'{parent_id}' in parents and name = '{name}' and "
        f"mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    resp = service.files().list(q=query, fields="files(id)").execute()
    existing = resp.get("files", [])
    if existing:
        return existing[0]["id"]
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    created = service.files().create(body=metadata, fields="id").execute()
    return created["id"]


def download_file_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def create_shortcut(service, target_id: str, name: str, parent_id: str) -> None:
    # ショートカットは実データを持たない参照のみのファイルなので、
    # サービスアカウントの保存容量を消費せずに作成できる。元の写真はそのまま残る。
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.shortcut",
        "shortcutDetails": {"targetId": target_id},
        "parents": [parent_id],
    }
    service.files().create(body=metadata, fields="id").execute()


# ---- Gemini評価 ------------------------------------------------------------

def score_single_image(image_bytes: bytes, mime_type: str) -> dict:
    """1枚の写真を技術面・構図面から0-100点で評価する。"""
    prompt = (
        "あなたは写真の目利きです。この写真を「後で見返したい・人に見せたい "
        "良い写真」かどうかという観点で0〜100点で評価してください。"
        "ピンボケ・手ブレ・露出過不足・目つぶり・構図の悪さは減点、"
        "被写体の魅力・タイミング・構図の良さは加点してください。"
        "必ず次のJSON形式のみで答えてください(説明文は不要): "
        '{"score": <0-100の整数>, "reason": "<20文字程度の短い理由>"}'
    )
    return _call_gemini(prompt, [(image_bytes, mime_type)])


def pick_best_in_group(images: list) -> dict:
    """同一バーストのグループから最良の1枚を選ばせる。images: [(bytes, mime, name), ...]"""
    prompt = (
        f"以下は同じ瞬間に連写された{len(images)}枚の写真です(1枚目から順に番号0,1,2...)。"
        "その中で最も良い1枚(ピンボケが少なく、表情や構図が良いもの)を選び、"
        "その写真自体の品質を0〜100点で評価してください。"
        "必ず次のJSON形式のみで答えてください: "
        '{"best_index": <0始まりの番号>, "score": <0-100の整数>, "reason": "<20文字程度の理由>"}'
    )
    parts = [(b, m) for b, m, _ in images]
    return _call_gemini(prompt, parts)


def _call_gemini(prompt: str, images: list, retries: int = 3) -> dict:
    parts = [{"text": prompt}]
    for image_bytes, mime_type in images:
        parts.append(
            {
                "inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }
            }
        )
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.2},
    }
    for attempt in range(retries):
        resp = requests.post(GEMINI_ENDPOINT, json=body, timeout=60)
        if resp.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        text = text.strip().strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
        return json.loads(text)
    raise RuntimeError("Gemini APIの呼び出しに繰り返し失敗しました(レート制限の可能性)")


# ---- メイン処理 -------------------------------------------------------------

def process_folder(service, folder_id: str, folder_name: str, processed: set) -> None:
    images = list_images(service, folder_id)
    new_images = [f for f in images if f["id"] not in processed]
    if not new_images:
        return

    print(f"[{folder_name}] 新規写真 {len(new_images)} 枚を確認")

    # バーストごとにグルーピング(BURSTが付かないものは単独グループ)
    groups: dict[str, list] = {}
    for f in new_images:
        m = BURST_RE.match(f["name"])
        key = m.group("base") if m else f["id"]
        groups.setdefault(key, []).append(f)

    selected_folder_id = None  # 必要になったときだけ作成する

    for key, group in groups.items():
        try:
            if len(group) == 1:
                f = group[0]
                image_bytes = download_file_bytes(service, f["id"])
                result = score_single_image(image_bytes, f["mimeType"])
                winner, score, reason = f, result["score"], result.get("reason", "")
            else:
                loaded = []
                for f in group:
                    b = download_file_bytes(service, f["id"])
                    loaded.append((b, f["mimeType"], f["name"]))
                result = pick_best_in_group(loaded)
                idx = result["best_index"]
                winner, score, reason = group[idx], result["score"], result.get("reason", "")

            print(f"  - {winner['name']}: score={score} ({reason})")

            if score >= SCORE_THRESHOLD:
                if selected_folder_id is None:
                    selected_folder_id = find_or_create_subfolder(
                        service, folder_id, SELECTED_FOLDER_NAME
                    )
                create_shortcut(service, winner["id"], winner["name"], selected_folder_id)
                print(f"    -> 「{SELECTED_FOLDER_NAME}」にショートカットを作成しました")

        except Exception as e:  # noqa: BLE001
            print(f"  ! グループ {key} の処理でエラー: {e}", file=sys.stderr)
            continue  # このグループは未処理のまま次回リトライさせる
        else:
            for f in group:
                processed.add(f["id"])


def main() -> None:
    service = get_drive_service()
    processed = load_processed_ids()

    folders = list_subfolders(service, ROOT_FOLDER_ID)
    print(f"{len(folders)} 個のイベントフォルダを処理します")

    for folder in folders:
        if folder["name"] == SELECTED_FOLDER_NAME:
            continue
        process_folder(service, folder["id"], folder["name"], processed)
        save_processed_ids(processed)  # フォルダごとに保存して途中経過を失わない

    print("完了しました")


if __name__ == "__main__":
    main()
