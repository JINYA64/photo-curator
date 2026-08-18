# 写真自動選別ボット セットアップ手順

## 全体の仕組み
1. GitHub Actionsが1日2回(朝8時・夜8時)自動実行される
2. 指定したGoogleドライブの親フォルダ配下にある各イベントフォルダを巡回
3. 新しく追加された写真だけをGemini APIで評価(連写はまとめて最良の1枚を選定)
4. 一定スコア以上の写真を、各イベントフォルダ内の「厳選」サブフォルダにコピー
5. 処理済みの写真IDを`processed_ids.json`に記録し、次回以降は同じ写真を評価しない

## 事前準備

### 1. Googleサービスアカウントを作成する(ドライブにアクセスするため)
1. [Google Cloud Console](https://console.cloud.google.com/) で新しいプロジェクトを作成(既存のものでも可)
2. 「APIとサービス」→「有効なAPI」から **Google Drive API** を有効化
3. 「IAMと管理」→「サービスアカウント」→「サービスアカウントを作成」
4. 作成後、そのアカウントの「鍵」タブから **JSON形式の鍵を作成**してダウンロード
5. ダウンロードしたJSONファイルの中の `client_email`(例: `xxx@xxx.iam.gserviceaccount.com`)をコピー
6. Googleドライブで、写真が入っている親フォルダ(今回教えていただいたフォルダ)を**そのメールアドレスに共有**する(閲覧者ではなく「編集者」権限で共有してください。厳選フォルダの作成・コピーが必要なためです)

### 2. Gemini APIキー
今治市ニュースボットで使っているものと同じAPIキーを使い回せます。新規に取得する場合は [Google AI Studio](https://aistudio.google.com/) から無料で発行できます。

### 3. 対象フォルダのID
教えていただいたURLから、フォルダIDは次の部分です:
```
https://drive.google.com/drive/folders/【ここがID】
1NPoT8h6__ueKdBtsl3q-B2gUH0Jf1YCh
```

## GitHubリポジトリへの設置(GitHubのWeb画面から)

1. 新しいリポジトリを作成する(今治市ニュースボットとは別のリポジトリを想定しています)
2. このやり取りで渡した以下のファイルをアップロードする:
   - `curate_photos.py`
   - `requirements.txt`
   - `.github/workflows/curate-photos.yml`
3. リポジトリの「Settings」→「Secrets and variables」→「Actions」→「New repository secret」で、以下の3つを登録:
   - `GOOGLE_SERVICE_ACCOUNT_JSON` : ダウンロードしたサービスアカウントのJSONファイルの中身をそのまま貼り付け
   - `GEMINI_API_KEY` : Gemini APIキー
   - `ROOT_FOLDER_ID` : `1NPoT8h6__ueKdBtsl3q-B2gUH0Jf1YCh`
4. 「Actions」タブを開き、ワークフロー「写真の自動選別」を選んで「Run workflow」から手動実行してみて、動作を確認する

## 調整できるポイント
- `SCORE_THRESHOLD`(現在70点): 厳しくしたい場合は80〜90に、多めに残したい場合は50〜60に調整してください(ワークフローファイル内の値を変更)
- 実行頻度: `curate-photos.yml` の `cron` の値を変更すれば頻度を変えられます
- 「厳選」フォルダは各イベントフォルダの中に自動で作られます。すでに写真が入っているフォルダに対しても、初回実行時にまとめて過去分も評価されます(件数が多いとAPIのレート制限にかかる場合があるため、まずは1フォルダなど少量で試すのがおすすめです)

## 注意点
- 動画ファイル(.mp4など)は対象外です(写真のみ処理します)
- Gemini APIの無料枠には呼び出し回数の上限があります。写真が非常に多い場合は、実行頻度や1回あたりの処理件数を調整してください
