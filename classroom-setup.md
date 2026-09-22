# Google ClassroomとDaily Morning Brief

Google Classroom公式APIから、本人が生徒として参加している開講中の授業について、課題・本人の提出状態・お知らせ・教材のタイトルと本文を取得します。添付ファイル、成績、他の生徒の提出内容は取得しません。

## 初回認証

1. [Google公式の設定手順](https://developers.google.com/workspace/classroom/quickstart/python#set_up_your_environment)に従って、Google CloudプロジェクトでClassroom APIを有効にします。
2. OAuth同意画面を設定し、種類が「デスクトップ アプリ」のOAuthクライアントを作成します。Externalのテスト用アプリなら、利用するClassroomアカウントをテストユーザーに追加します。
3. クライアントJSONをダウンロードし、リポジトリ外に保存します。JSONの内容やパスワードをチャットへ貼る必要はありません。
4. 初回だけ、PowerShellで次を実行します。

```powershell
& '.\scripts\install-classroom.ps1'
& '.\scripts\setup-classroom-auth.ps1' -ClientSecretPath 'C:\path\outside\the\repo\client_secret.json'
```

表示されたGoogleの画面で、Classroomを利用しているアカウントを選び、読み取り専用アクセスを許可します。クライアント作成用のGoogle Cloudアカウントと、Classroomの利用アカウントは別でも構いません。学校側のアプリ制限で拒否された場合は、管理者による許可が必要です。

認証処理は公式のgoogle-auth-oauthlibを使い、更新トークンをWindows資格情報マネージャーの`Codex:DailyBrief:GoogleClassroom`に保存します。平文のtoken.jsonは作成しません。定期実行では認証画面を開かず、保存した認証の更新だけを行います。

必要な権限はclassroom.courses.readonly、classroom.coursework.me.readonly、classroom.announcements.readonly、classroom.courseworkmaterials.readonlyの4つです。

## 収集と読取

```powershell
& '.\scripts\run-classroom-collector.ps1'
$classroomCollectorExit = $LASTEXITCODE
& '.\scripts\read-classroom-briefing-input.ps1'
```

収集は成功・部分成功で0、認証・環境設定待ちで2、それ以外の失敗で1を返します。収集が失敗しても読取を実行してください。読取JSONはsuccess、partial、stale、unavailableを区別します。認証前はunavailableとなり、Daily briefは残りの情報源で続行します。

## 差分の扱い

- APIの全ページを取得して、授業・構成要素ごとの前回正常取得と比較します。更新日時だけでなく本文・締切・本人の提出状態も比較するため、同じ更新日時の変更も検出します。サーバー側の差分トークンや更新日時フィルターは使用しません。
- `data.changes`にadded、updated、removedを出力します。removedは公開一覧から見えなくなった意味で、削除や提出完了を断定しません。
- `data.assignments`は現在の課題一覧を保持し、期限接近の確認に使えます。お知らせと教材は追加・更新分だけを読取JSONへ出力します。
- 一部取得が失敗した場合、その構成要素の前回データと成功時刻を維持します。次の成功時に未取得だった変更を検出します。全体失敗時は正常キャッシュを上書きせず、今回取得失敗の印を別に保存して、古いデータであることを読取JSONに示します。
- 課題のreturnedは返却済みであり、再提出指示とは限りません。unknownは未確認です。締切はAPIのUTC日時を保存し、ブリーフィングで日本時間へ変換します。

キャッシュは`%LOCALAPPDATA%\ClassroomMorningBriefing`、依存ライブラリはこのプロジェクトの`.venv-classroom`に保存します。LoiLoNoteのキャッシュや認証情報とは独立しています。

## 検証

```powershell
& '.\.venv-classroom\Scripts\python.exe' -X utf8 -m unittest discover -s tests -q
```

差分、提出状態だけの更新、締切変更、ページ送り、部分失敗後の回復、全体失敗時の保護、UTC日時、公式SDKのGET要求と本人限定フィルターを検証します。2026-09-13時点で実アカウントの取得は初回OAuth認証待ちです。

API仕様: [課題一覧](https://developers.google.com/workspace/classroom/reference/rest/v1/courses.courseWork/list)、[本人の提出状態](https://developers.google.com/workspace/classroom/reference/rest/v1/courses.courseWork.studentSubmissions/list)、[お知らせ](https://developers.google.com/workspace/classroom/reference/rest/v1/courses.announcements/list)、[教材](https://developers.google.com/workspace/classroom/reference/rest/v1/courses.courseWorkMaterials/list)。
