# Clip Extractor Premiere Bridge

Clip Extractor がローカルで書き出した動画を Premiere Pro に渡すための
UXP companion plugin です。

## 対応環境

- Adobe Premiere Pro 25.6 以降
- Clip Extractor と Premiere Pro が同じPC上で動作していること

## インストール

Clip Extractor の Settings または Output にある
「Premiere連携プラグインをインストール」を押します。Creative Cloud の確認画面で
インストールとローカルファイルアクセスを許可し、Premiere Pro を再起動します。

プラグインは Premiere 起動時にバックグラウンドで開始し、
`127.0.0.1` の専用ポートだけを確認します。外部ネットワークへは接続しません。

## 動作

Clip Extractor で切り抜きを書き出した後に「Premiere Proで編集」を押すと、
書き出し済みファイルだけを現在のPremiereプロジェクトへ読み込みます。
プロジェクトが開かれていない場合は、書き出し先に新しい `.prproj` を作成します。
各動画からシーケンスを作成し、最初のシーケンスを開きます。
