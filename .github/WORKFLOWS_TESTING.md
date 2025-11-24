# GitHub Actions 測試指南

本文件說明如何使用 `act` 在本地測試 GitHub Actions workflows。

## 安裝 act

如果尚未安裝，請參考：https://github.com/nektos/act

```powershell
# Windows (使用 Chocolatey)
choco install act-cli

# 或使用 Scoop
scoop install act
```

## Workflows 概覽

我們有 4 個自動化 workflows：

1. **auto-version-tag.yml** - 在 develop/feature 分支偵測版本變更並自動打標籤
2. **hotfix-tag.yml** - 在 hotfix 分支自動創建 patch 標籤
3. **release.yml** - 在 release 合併到 main 時打穩定版標籤
4. **update-version-history.yml** - 當創建標籤時更新版本歷史

## 測試方式

### 1. 測試 auto-version-tag.yml (版本變更自動標籤)

```powershell
# 模擬在 develop 分支修改 config.yaml
act push -W .github/workflows/auto-version-tag.yml `
  --eventpath .github/act-events/push-develop-config.json `
  --container-architecture linux/amd64
```

需要創建事件檔案 `.github/act-events/push-develop-config.json`:
```json
{
  "ref": "refs/heads/develop",
  "repository": {
    "name": "U.E.P-s-Core",
    "owner": {
      "login": "Unforgettableeternalproject"
    }
  }
}
```

### 2. 測試 hotfix-tag.yml (Hotfix 自動標籤)

```powershell
# 模擬在 hotfix/v0.7.4 分支提交
act push -W .github/workflows/hotfix-tag.yml `
  --eventpath .github/act-events/push-hotfix.json `
  --container-architecture linux/amd64
```

事件檔案 `.github/act-events/push-hotfix.json`:
```json
{
  "ref": "refs/heads/hotfix/v0.7.4",
  "ref_name": "hotfix/v0.7.4",
  "repository": {
    "name": "U.E.P-s-Core",
    "owner": {
      "login": "Unforgettableeternalproject"
    }
  }
}
```

### 3. 測試 release.yml (穩定版標籤)

```powershell
# 模擬 PR 合併到 main
act pull_request -W .github/workflows/release.yml `
  --eventpath .github/act-events/pr-release-merge.json `
  --container-architecture linux/amd64
```

事件檔案 `.github/act-events/pr-release-merge.json`:
```json
{
  "action": "closed",
  "pull_request": {
    "merged": true,
    "head": {
      "ref": "release/v0.7.4"
    },
    "base": {
      "ref": "main"
    }
  },
  "repository": {
    "name": "U.E.P-s-Core",
    "owner": {
      "login": "Unforgettableeternalproject"
    }
  }
}
```

### 4. 測試 update-version-history.yml (版本歷史更新)

```powershell
# 模擬創建標籤
act push -W .github/workflows/update-version-history.yml `
  --eventpath .github/act-events/tag-created.json `
  --container-architecture linux/amd64
```

事件檔案 `.github/act-events/tag-created.json`:
```json
{
  "ref": "refs/tags/v0.7.5",
  "ref_name": "v0.7.5",
  "repository": {
    "name": "U.E.P-s-Core",
    "owner": {
      "login": "Unforgettableeternalproject"
    }
  }
}
```

## 快速測試腳本

創建 `test-workflows.ps1`:

```powershell
# 測試所有 workflows

Write-Host "🧪 測試 auto-version-tag workflow..." -ForegroundColor Cyan
act push -W .github/workflows/auto-version-tag.yml `
  --eventpath .github/act-events/push-develop-config.json `
  --container-architecture linux/amd64 `
  --dryrun

Write-Host "`n🧪 測試 hotfix-tag workflow..." -ForegroundColor Cyan
act push -W .github/workflows/hotfix-tag.yml `
  --eventpath .github/act-events/push-hotfix.json `
  --container-architecture linux/amd64 `
  --dryrun

Write-Host "`n🧪 測試 release workflow..." -ForegroundColor Cyan
act pull_request -W .github/workflows/release.yml `
  --eventpath .github/act-events/pr-release-merge.json `
  --container-architecture linux/amd64 `
  --dryrun

Write-Host "`n✅ 所有 workflow 測試完成" -ForegroundColor Green
```

## 常用選項

- `--dryrun`: 只顯示將執行什麼，不實際執行
- `--verbose`: 顯示詳細日誌
- `--container-architecture linux/amd64`: 指定容器架構（M1/M2 Mac 需要）
- `-s GITHUB_TOKEN=<token>`: 提供 GitHub token（如果需要）
- `--list`: 列出所有可執行的 jobs

## 故障排除

### 問題：找不到 git 倉庫
```powershell
# 確保在專案根目錄執行
cd C:\Users\Bernie\source\repos\Unforgettableeternalproject\U.E.P-s-Core
```

### 問題：權限錯誤
```powershell
# 使用 --privileged 選項
act push --privileged -W .github/workflows/auto-version-tag.yml
```

### 問題：網路超時
```powershell
# 增加超時時間
act push --env ACT_TIMEOUT=600 -W .github/workflows/auto-version-tag.yml
```

## 實際使用流程

### 開發新功能時 (feature 分支)
1. 創建功能分支：`git checkout -b feature/new-feature`
2. 開發完成後，更新版本號：修改 `configs/config.yaml` 中的 `system_version`
3. 提交：`git commit -m "feat: 新功能"`
4. 推送：`git push` → 自動打標籤 ✅

### 發布穩定版時 (release → main)
1. 創建 release 分支：`git checkout -b release/v0.8.0`
2. 完成測試後，創建 PR 到 main
3. 合併 PR → 自動打 `v0.8.0-stable` 標籤 ✅
4. 自動更新 VERSION_HISTORY.md ✅

### 緊急修復時 (hotfix 分支)
1. 從 main 創建 hotfix：`git checkout -b hotfix/v0.7.4`
2. 修復問題並提交：`git commit -m "fix: 緊急修復"`
3. 推送：`git push` → 自動打 `v0.7.4-patch-1` 標籤 ✅

## 版本標籤規則總結

| 分支類型 | 標籤格式 | 範例 | 觸發條件 |
|---------|---------|------|---------|
| develop/feature/* | `v{version}` | `v0.7.5` | config.yaml 版本變更 |
| release/* → main | `v{version}-stable` | `v0.7.5-stable` | PR 合併到 main |
| hotfix/* | `v{version}-patch-{num}` | `v0.7.4-patch-1` | 任何提交到 hotfix 分支 |

---

*最後更新: 2025-11-24*
