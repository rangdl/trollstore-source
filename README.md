# trollstore-source

一个给 **TrollStore（巨魔）** 用的 [AltStore 格式](https://github.com/AltStore/SourceRepository)
软件源，配 [TrollApps](https://github.com/TheResonanceTeam/TrollApps) 使用：在商店里浏览、搜索、一键下载并安装，不用再一个个手动把 `.ipa` 交给 TrollStore。

源里的应用分两类：

- **跟随上游 release（auto）** —— 例如 [Codeg for iOS](https://github.com/rangdl/codeg-ios)，自动指向它最新的 GitHub Release，无需手工维护
- **固定版本（manual）** —— 自己写 `versions` 数组，适合不在 GitHub 发布的应用

## 源地址

国内加速（推荐，所有链接走镜像）：

```
https://gh-proxy.com/https://raw.githubusercontent.com/rangdl/trollstore-source/main/source-cn.json
```

jsDelivr（返回 `application/json`，国内一般可达）：

```
https://cdn.jsdelivr.net/gh/rangdl/trollstore-source@main/source.json
```

GitHub 直连（需要能访问 `github.com`）：

```
https://raw.githubusercontent.com/rangdl/trollstore-source/main/source.json
```

## 在 TrollApps 里加源

1. 打开 **TrollApps → Sources → +**
2. 粘贴上面任意一条地址（国内用第一条）并确认
3. 应用会出现在源下面 —— 点进去，按 **Get / Install**，TrollApps 会把 IPA 交给 TrollStore 永久安装

> TrollStore 需要在 **设置里打开 `URL Scheme Enabled`**，否则 TrollApps 无法调起安装。

## 添加新应用

### 方式一：直接从 IPA 生成（推荐）

```bash
scripts/add-app.py https://example.com/SomeApp.ipa \
    --developer "作者" --subtitle "一句话简介" --category utilities
```

它会读 IPA 里的 `Info.plist`，自动拿到 bundle id、版本号、显示名、最低 iOS 版本和使用权限说明，并把图标提取出来（IPA 里的图标是 Apple 私有的 CgBI 变体，脚本会解码后重新编码成标准 PNG）。然后写入 `apps/<bundle id>.json` + `icons/<bundle id>.png`，并重建源。

想让它以后自动跟随某个仓库的 release：

```bash
scripts/add-app.py <ipa-url> \
    --auto-repo owner/repo --auto-asset App-unsigned.ipa --min-os 15.0
```

资产名里带版本号的仓库（例如 `App-v20260929.ipa`，每次发版名字都变），用正则匹配并从资产名抽版本号：

```bash
scripts/add-app.py <ipa-url> \
    --auto-repo owner/repo \
    --auto-tag latest \
    --auto-asset-pattern 'App-v(\d{8})\.ipa' \
    --auto-version-pattern 'v(\d{8})' \
    --version-from-asset \
    --min-os 15.0
```

- `--auto-tag`：跟踪指定 tag 而不是仓库的最新正式 release（适合滚动的 `latest` 预发布）
- `--auto-asset-pattern`：资产名正则，多个匹配时取上传时间最新的那个
- `--version-from-asset`：让 `--auto-version-pattern` 匹配资产名而非 tag，且版本日期取资产的上传时间

### 方式二：手写一个 app 文件

在 `apps/` 下新建 `任意名字.json`：

```json
{
  "name": "示例应用",
  "bundleIdentifier": "com.example.app",
  "developerName": "作者",
  "subtitle": "一句话简介",
  "localizedDescription": "长描述……",
  "icon": "icons/com.example.app.png",
  "tintColor": "#1E1E36",
  "category": "utilities",
  "versions": [
    {
      "version": "1.2.3",
      "date": "2026-01-01",
      "downloadURL": "https://example.com/App.ipa",
      "size": 12345678,
      "minOSVersion": "15.0",
      "localizedDescription": "这一版改了什么"
    }
  ]
}
```

放好图标后重建源：

```bash
scripts/build_source.py
```

`versions` 数组里**最新的一版放最前面**（客户端把 `versions[0]` 当作当前版本）。

`auto` 块支持的可选字段（对应上面脚本的同名参数）：

| 字段 | 作用 |
|---|---|
| `tag` | 跟踪指定 tag，而不是仓库的最新正式 release（滚动的 `latest` 预发布就填它） |
| `assetPattern` | 资产名正则，多个匹配时取上传时间最新的；填了它就不用 `asset` |
| `versionFromAsset` | 让 `versionPattern` 匹配资产名而非 tag，版本日期取资产的上传时间 |
| `versionPattern` | 版本号正则，第 1 个捕获组作为版本号（默认 `^v?(\d+\.\d+\.\d+)`） |

## 更新日志

 TrollApps 的应用详情页会把 `versions[].localizedDescription` 显示为 **WHATS NEW**，切换历史版本还能看各自的说明。带 `auto` 的应用会自动把上游 GitHub Release 的正文（去掉 markdown 语法、图片和超长截断）填进去，无需手工维护；手动应用直接在 `versions[].localizedDescription` 里写即可。

## 自动刷新

`apps/*.json` 里带 `auto` 的应用由 GitHub Actions 维护
（[`.github/workflows/refresh.yml`](.github/workflows/refresh.yml)）：每 6 小时跑一次，也会在手动触发或收到 `app-release` 的 `repository_dispatch` 时跑。它重建 `source.json` / `source-cn.json`，有变化才提交 —— 你不需要手动更新。

想立刻刷新（例如刚发布了新版）：

```bash
gh workflow run refresh.yml -R rangdl/trollstore-source
```

别的仓库发布后想马上通知这个源：

```bash
gh api repos/rangdl/trollstore-source/dispatches \
  -f event_type=app-release -f client_payload[app]=codeg
```

## 目录结构

| 路径 | 说明 |
|---|---|
| `repo.json` | 源本身的元数据（名称、标识、描述、图标、tintColor） |
| `apps/*.json` | 每个应用一份描述（手工维护或 `add-app.py` 生成） |
| `icons/*.png` | 应用图标 + 源图标 |
| `scripts/build_source.py` | 由 `repo.json` + `apps/*.json` 生成源 JSON |
| `scripts/add-app.py` | 从 IPA 生成一个 app 文件 |
| `source.json` / `source-cn.json` | **生成物**，不要手改 |

重建源（会覆盖上面两个生成物）：

```bash
scripts/build_source.py                 # 直连版 + 加速版
PROXY= scripts/build_source.py          # 只要直连版
scripts/build_source.py --proxy https://ghfast.top/   # 换一个加速镜像
scripts/build_source.py --check         # 只检查是否最新（CI 用）
```

## 注意

源里的 IPA 都是**未签名**的，只能装在已经装了 TrollStore 的设备上，且系统版本要在 TrollStore 支持的范围内。安装来源不明的 IPA 有风险，加应用前请自行确认来源。
