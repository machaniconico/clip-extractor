# Windows 版の無償配布

個人・無償の Windows 向けソース ZIP を GitHub Release で配布します。

1. 必要な変更をコミットし、CI が成功したコミットにタグを付けます。
   `git tag -a v1.0.0 -m "Clip Extractor v1.0.0"`
2. `git push origin v1.0.0` で Release ワークフローを起動します。
   手動実行は Actions → Release → Run workflow で既存タグを入力します。
   タグ名には英数字で始まる英数字・ピリオド・ハイフン・アンダースコアを使います。
3. タグの内容を `git archive` で ZIP 化し、内部管理ディレクトリを除外します。
   禁止ファイルの混入と setup.bat / Clip Extractor.bat の CRLF を検査します。
   展開物の秘密検査、ライセンス通知確認、Bandit、pip-audit を実行します。
   展開物の requirements.lock をインストールした環境から SBOM を生成します。
4. 全検査が成功すると ZIP・CycloneDX SBOM・SHA256SUMS.txt 付き draft ができます。
   ZIP を Windows 上で展開し、セットアップと起動を確認してください。
   自動検査は Windows の実動作確認を代替しません。
5. Releases の draft を開き、タグ・添付物・説明を確認して人間が Publish release を押します。
   ワークフローは公開しません。

失敗時は Actions → Release → 対象実行で赤いステップのログを確認します。
検査失敗は修正して新しいタグを作成し、再実行してください。
既存 Release は上書きしません。添付途中で失敗した場合は draft の状態を確認し、
不要な draft を人間が削除してから同じタグで手動実行します。
