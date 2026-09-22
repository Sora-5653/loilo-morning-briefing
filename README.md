# LoiLoNote朝ブリーフィング収集器

LoiLoNote Schoolの課題と授業タイムラインを、画面操作なし・読み取り専用で朝ブリーフィングへ渡すWindows向け収集器です。オプションを指定すると、授業の「共有ノート」一覧とフォルダー内の共有ノートからもカードを抽出できます。朝の定期実行はブラウザー、Chrome、Playwright、画面解析を使いません。

## 現在の状態

- 非UIのHTTP収集器: 実装済み
- タイムラインのテキストカード抽出: 実装済み
- 共有ノート一覧のテキストカード抽出: オプションとして実装済み
- 認証情報のWindows資格情報マネージャー保存: 実装済み
- キャッシュ保護、差分カーソル、部分成功: 実装済み
- 単体試験: 実装済み
- 実アカウントでの読取確認: 完了

共有ノート収集は既定で無効です。既存の朝ブリーフィング用スクリプトと定期実行設定は、共有ノートを収集しません。

2026-09-13に、APIが返す空題名の課題を構造エラーとして除外していた問題を修正しました。題名は空文字のまま保持し、課題ID・締切・本人の提出状態を取得します。題名から内容を推測しません。題名フィールドの欠落、不正な型、通信失敗は引き続きエラーとして扱います。

収集・入力読取スクリプトは、Python 3.11以上が実行できることを確認してから処理を開始します。`py` が利用不能なら、ユーザーのPythonインストール、PATH上のPythonを順に確認します。入力JSONはUTF-8で出力するため、絵文字を含む連絡もWindowsの既定文字コードに依存しません。

## 初回認証

Google Classroomの収集・認証手順は[Classroomの設定](classroom-setup.md)を参照してください。Classroomは独立した読み取り専用の情報源です。OAuth認証が未完了でもGmailとLoiLoNoteの定期実行は続行します。

初回認証にはNode.js 20以上とPlaywrightが必要です。リポジトリのルートで一度だけ依存関係を導入します。

```powershell
npm install
```

その後、通常のPowerShellで次を明示的に実行します。

```powershell
powershell -ExecutionPolicy Bypass -File '.\scripts\setup-loilo-auth.ps1'
```

一時ブラウザーに公式LoiLoNoteログイン画面が表示されます。ログイン操作は利用者自身が行います。パスワード、学校ID、多要素認証コードをこのツールやリポジトリへ保存しないでください。補助処理は認証済みセッションCookieをWindows資格情報マネージャーへ保存し、一時ブラウザーを破棄します。

これは初回認証と失効時の再認証だけに使います。朝の定期タスクはこの処理を起動しません。

## 手動確認

収集を実行します。

```powershell
powershell -ExecutionPolicy Bypass -File '.\scripts\run-loilo-collector.ps1'
```

授業の共有ノートも収集するには、`-IncludeSharedNotes` を指定します。

```powershell
powershell -ExecutionPolicy Bypass -File '.\scripts\run-loilo-collector.ps1' -IncludeSharedNotes
```

PythonのCLIを直接使う場合は、次のコマンドを実行します。

```powershell
py -3 -m loilo_briefing collect --include-shared-notes
```

このオプションを指定した実行結果には、`shared_notes` 配列が追加されます。各要素には授業、ノート名、作成日時、更新日時、カードが含まれます。変更されていない共有ノートは、検証済みキャッシュのカードを再利用します。オプションを指定しない実行結果には、`shared_notes` を含めません。

収集全体が失敗した場合は、データ損失を避けるため、直前の正常なキャッシュをそのまま保持します。直前の成功実行で共有ノートオプションを指定していた場合、その保護済みキャッシュには `shared_notes` が残ります。

ブリーフィングへ渡す検証済みJSONを確認します。

```powershell
powershell -ExecutionPolicy Bypass -File '.\scripts\read-loilo-briefing-input.ps1'
```

認証状態だけを確認する場合:

```powershell
py -3 -m loilo_briefing auth status
```

保存したセッションを削除する場合:

```powershell
py -3 -m loilo_briefing auth clear
```

## 終了コード

`collect` は、成功または部分成功で0、認証が必要な場合は2、その他の全体失敗は1を返します。失敗しても既存の正常なキャッシュは上書きしません。

## 試験

外部パッケージなしで実行できます。

```powershell
py -3 -m unittest discover -s tests -v
```

試験には、通信再試行、GET限定、認証切れ、構造変更、検証済み文書配信先への一段取得、Cookie・APIトークンの非転送、文書ZIP、実データ形式のテキストカード、共有ノートとフォルダーのページ処理、共有ノートスナップショット、主要カード種別、機密URL除外、原子的キャッシュ、一部・全体失敗、差分カーソル、ブラウザーへの実行時フォールバック禁止が含まれます。

詳しい設計と受入条件は [morning-brief-loilonote-spec.md](morning-brief-loilonote-spec.md) を参照してください。

