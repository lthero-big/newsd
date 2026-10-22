# 开发

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest          # 测试全部使用模拟 HTTP，不需要联网
.venv/bin/ruff check .    # 代码检查
```

每次 push 和 Pull Request 都会在 GitHub Actions 上运行 ruff、pytest（Python 3.11 / 3.12）和 gitleaks 密钥扫描。更细的设计文档在 [docs/](https://github.com/lthero-big/newsd/blob/main/docs/)。
