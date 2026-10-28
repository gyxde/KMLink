# 开发与发布

日常修改从 `develop` 新建功能分支，测试通过后提交 PR 合并到 `develop`。准备发布时，把经过验证的 `develop` 合并到 `main`。

安装和运行见 README。提交前运行：

```bash
python -m unittest discover -s tests -v
```

发布版本时，同步更新 `pyproject.toml` 的版本和 `src/kmlink/__init__.py` 的 `__version__`，以及 `android/app/build.gradle` 的 `versionName` / 递增的 `versionCode`。在 `main` 对应提交创建 `vX.Y.Z` 标签并推送，GitHub Actions 会分别构建 Windows x64、Mac arm64 和 Mac Intel，然后创建 Release。不要移动已经发布的版本标签；修复需要发布新版本。

```bash
git switch main
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin main develop
git push origin vX.Y.Z
```

正式 Release 的下载文件名保持固定，版本由 Release 标签标识，README 的 latest 链接自动指向最新正式版。开发分支的构建只作为 Actions Artifact 保留，不覆盖正式下载。

不要提交 `.venv/`、`.vendor/`、`build/`、`dist/` 或 `artifacts/`。截图和调试日志可能包含本机名称、局域网地址，上传前应去除这些信息。软件构建产物应放在 Releases，而不是 Git 源码历史中。
