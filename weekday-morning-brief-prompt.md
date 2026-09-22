# Daily Morning Brief 実行プロンプト

「Daily Morning Brief」という朝のブリーフィングを日本語で作成してください。

## 情報源と独立性

ローカル収集器を使う実行環境では、環境変数 `MORNING_BRIEF_ROOT` にこのリポジトリのルートを設定します。

情報源はGmail、LoiLoNote School、Google Classroomの3つです。Slackは使用しません。一部の情報源の取得が失敗しても、成功した情報源の実データだけでブリーフィングを作成します。ダミー、例示、推測で欠損を埋めてはいけません。

## Gmail

- 接続済みGmailのライブデータだけを使います。
- メインの受信トレイを `in:inbox after:YYYY/MM/DD -in:spam -in:trash -category:promotions -category:social` 相当で検索します。
- 初回は過去3日間、通常はGmail側の前回成功時刻以降を対象にします。
- まず件名、送信者、受信時刻で候補を絞り、重要度判定に必要な候補だけ本文を読みます。
- メールの既読化、ラベル変更、移動、削除、返信、転送などの書き込みを行いません。

## LoiLoNote School

### 絶対条件

- Codexのブラウザー操作、Chrome操作、画面クリック、画面要素探索、画面解析を一切行いません。
- 画面操作へのフォールバックを行いません。認証切れ、通信失敗、構造変更のどの場合も同じです。
- `scripts/setup-loilo-auth.ps1` は人が初回認証を設定するための補助処理です。定期実行中に自動で起動してはいけません。
- LoiLoNoteへの書き込み操作を行いません。収集器が許可するHTTPSのGET要求だけを使います。

### 取得手順

次のローカル収集器を一度実行します。終了コードが0以外でも処理全体を中断せず、必ず次の入力読取処理まで進みます。

```powershell
$briefingRoot = (Resolve-Path $env:MORNING_BRIEF_ROOT).Path
& (Join-Path $briefingRoot 'scripts\run-loilo-collector.ps1')
$loiloCollectorExit = $LASTEXITCODE
& (Join-Path $briefingRoot 'scripts\read-loilo-briefing-input.ps1')
```

2番目の処理が標準出力へ返すJSONだけをLoiLoNoteの入力として使用します。キャッシュファイルや認証情報を直接読みません。

- `status` が `success` または `partial` の場合、`data.assignments` と `data.timeline` を使用します。
- `status` が `stale` の場合、内容は利用できますが、最終結果の末尾へ「LoiLoNoteは最終取得から約N時間経過したキャッシュです」と一行付記します。
- `status` が `unavailable` の場合、LoiLoNoteを取得失敗として扱います。再認証の画面を開かず、取得できた他の情報源で続行します。
- `data.course_status` で一部授業または一部構成要素が `failed` の場合、取得できた範囲だけを使い、一行の注記を付けます。
- `errors` は種類名だけを扱います。内部URL、クエリ、Cookie、トークンなどを表示しません。

`data.timeline[].cards` は次のように解釈します。

- `type: text`: `text` を教員・授業からの連絡本文として要約対象にします。
- `type: image`: 画像カードがある事実と安全なメタデータだけを扱います。OCRは行いません。
- `type: pdf/document`: 文書カードがある事実と安全なメタデータだけを扱います。本文抽出を推測しません。
- `type: web/link`: 安全なURLがJSONに含まれる場合だけ参照します。認証情報らしいクエリを含むURLは収集器が省略します。
- `type: unknown`: 種類不明と明示し、内容を推測しません。

タイムライン本文から、行事、持ち物、時間割変更、休校・登校、健康・安全、保護者対応、期限付き手続きなど、今日の行動に影響する連絡を優先します。単なる教材共有や雑談は重要度が低いものとして扱います。

`data.assignments` からは、新規課題、24時間以内の締切、7日以内の締切、未提出、遅延、再提出が必要な課題を優先します。`status: unknown` を未提出と断定してはいけません。締切がない場合も推測しません。

## Google Classroom

### 取得手順

Google Classroomは公式APIを使用するローカル収集器から読み取り専用で取得します。通常実行でブラウザー・画面操作・再認証を行いません。初回認証専用のsetup-classroom-auth.ps1を自動起動しません。課題提出、既読化、編集、削除、コメント投稿、権限変更を行いません。

次の収集器を一度実行し、終了コードが0以外でも必ず入力読取を実行します。GmailとLoiLoNoteの成功・失敗にかかわらず、この取得手順まで進みます。

```powershell
$briefingRoot = (Resolve-Path $env:MORNING_BRIEF_ROOT).Path
& (Join-Path $briefingRoot 'scripts\run-classroom-collector.ps1')
$classroomCollectorExit = $LASTEXITCODE
& (Join-Path $briefingRoot 'scripts\read-classroom-briefing-input.ps1')
```

2番目の処理が標準出力に返すJSONだけをClassroomの入力に使用します。認証情報やキャッシュを直接読みません。

- statusがsuccessまたはpartialなら、取得できたdata.assignments、data.announcements、data.materials、data.changesを使います。
- data.assignmentsは締切確認用の現在一覧です。data.changesにあるadded/updatedを新規・更新の判定に使用します。変化のない課題は締切接近などの根拠がある場合だけ再掲します。
- data.announcementsとdata.materialsは前回の正常取得から追加・更新された項目だけです。初回は過去3日程度の連絡を中心に選別します。本文は各項目のtext、リンクは安全なurlを使います。添付本文を取得したとみなしたり、タイトルから補ったりしません。
- 各授業・構成要素の取得状態はdata.course_statusで確認します。statusがfailedの構成要素は前回データまたは未取得です。現在の提出状態・締切を確認済みと断定せず、部分取得を一行注記します。
- 課題のstatusはsubmitted、not_submitted、returned、unknownです。returnedは返却済みであり、再提出必要とは限りません。unknownを未提出と断定しません。lateは提出済みでも真になり得るため、未提出の判定には使いません。
- deadlineはUTCの日時です。Asia/Tokyoに変換して表示します。nullの場合は締切を推測しません。
- changesのremovedはAPIの公開一覧から見えなくなったことを示します。削除や提出完了とは断定しません。
- statusがstaleなら古いデータとして扱い、last_attempt_failedがtrueなら「Google Classroomは今回の取得に失敗したため、前回取得分です」と注記します。それ以外は最終取得からの経過時間を注記します。
- statusがunavailableならClassroomを未確認とし、取得できた他の情報源で続行します。初回認証待ちでもブリーフィング全体は止めません。
- 学校・ユーザーの内部ID、OAuth情報、APIエラー本文、他の生徒の情報を出力しません。

## 取得時間と状態

- Gmail、LoiLoNote、Google Classroomの成功時刻は別々に扱います。一方の失敗で他方の成功時刻を進めません。
- 同じ項目を毎日再掲しません。ただし、締切接近、提出状態の変化、重要な追記があれば再掲できます。
- LoiLoNoteのタイムラインは収集器のカーソルより新しい項目を基本とします。初回取得では取得結果のうち過去3日程度を中心に選別します。

## 重要度

次の順で優先します。

1. 締切超過、本人の未提出または再提出必要が明示された課題
2. 24時間以内の締切で、本人の提出済みを確認できない課題
3. 今日の行動に影響する学校連絡
4. 修正または再提出が明示された課題
5. アカウント、安全、請求、学校手続きなど期限付きの重要Gmail
6. 7日以内の締切で、本人の提出済みを確認できない課題
7. 新規公開された課題または重要なタイムライン連絡

未読であることだけを緊急性の根拠にしません。タイトルや教科名だけから内容を推測しません。

## 重複排除

同じ学校連絡がGmail、LoiLoNote、Google Classroomの複数にある場合は、授業・送信者、タイトル、締切、公開時刻、本文要旨から確実に同一と判断できる場合だけ一件に統合します。課題状態と締切はその課題の配信元（LoiLoNoteまたはGoogle Classroom）を主情報とし、Gmailにしかない説明があれば補足します。

## 出力

返答は日本語の最終回答一つだけにします。コネクタ確認、収集器の実行過程、検索メモ、内部処理、使用した道具を表示しません。

```markdown
# 朝のブリーフィング

## 重要項目

- **[LoiLoNote] 授業名「課題名または連絡名」— 締切または公開時刻**
  - 理由: ...
  - 次の行動: ...
  - 緊急度: ...

- **[Google Classroom] 授業名「課題名または連絡名」— 締切または公開時刻**
  - 理由: ...
  - 次の行動: ...
  - 緊急度: ...

- **[Gmail] 件名**
  - 理由: ...
  - 次の行動: ...
  - 緊急度: ...

## 後で / 参考
- 必要な場合だけ最大3件。
```

- `重要項目` は3情報源の合計で通常3〜5件を目安にします。重要案件が5件を超える場合は省略しません。
- 重要項目がない場合は「今朝、新たに対応が必要な重要事項は見つかりませんでした」と伝えます。
- 一部の情報源を取得できない場合は成功した情報源だけで作成し、末尾に「注: 今回は〇〇を確認できませんでした。△△の確認結果のみです」と一行付けます。
- 全情報源を取得できない場合は「今朝はGmail、LoiLoNote、Google Classroomの実データを取得できなかったため、ブリーフィングを作成できませんでした」とだけ伝えます。
- 個人情報、学校ID、生徒ID、Cookie、トークン、認証情報、他の生徒の内容を出力しません。
- カレンダー予定は作成しません。

