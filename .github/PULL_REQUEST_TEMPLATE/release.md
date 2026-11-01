## Release version vX.Y.Z deploy to main. MM/DD

<!-- ⬆️ 請直接修改上方標題作為 PR 標題 -->

### 版本資訊

- **Release 版本**: vX.Y.Z
- **對應 branch**: `release/vX.Y.Z`
- **config.yaml system_version**: <!-- 確認已更新 -->

### 變更摘要

<!-- 列出此版本的主要變更 -->

#### 新功能

-

#### 修復

-

#### 重構 / 其他

-

### 部署確認

- [ ] `configs/config.yaml` 版本號已更新至 vX.Y.Z
- [ ] 單元測試通過（`python -m pytest unit_tests/ -v`）
- [ ] 整合測試通過（`python -m pytest integration_tests/ -v`）
- [ ] Lint 通過
- [ ] staging 已驗證通過（如適用）

### 合併方式

> ⚠️ 請使用 **Squash and merge**
> CI 會自動：建立 `vX.Y.Z-stable` tag → sync back to develop → 建立 GitHub Release
