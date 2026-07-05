# 客户 Agent 使用提示词

请安装并使用 `illustrator-manual-translator` skill。

先运行安装命令：

```bash
curl -fsSL https://raw.githubusercontent.com/HiOne0826/illustrator-manual-translator-skill/main/install.sh | bash
```

如果仓库是私有仓库，请使用带 GitHub token 的安装命令：

```bash
export GITHUB_TOKEN="github_pat_xxx"
curl -fsSL \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://raw.githubusercontent.com/HiOne0826/illustrator-manual-translator-skill/main/install.sh \
  | GITHUB_TOKEN="$GITHUB_TOKEN" bash
```

安装完成后，用该 skill 处理我的 Adobe Illustrator 说明书文件：

1. 先运行 `doctor` 检查本机环境。
2. 对源 `.ai` 文件执行 `export`，导出 `textframes.json` 和 `textframes.md`。
3. 用 `template` 生成目标语言翻译 JSON。
4. 填充 `targetText` 后，用 `apply` 写入 `.ai` 副本，不要修改源文件。
5. 导出 PDF 后执行 `verify`。
6. 最后交付 `.ai` 副本、PDF、`replace-report.md` 和 `verify-report.md`。

重要限制：

- 不要直接覆盖源 `.ai` 文件。
- 只能直接替换 Illustrator `TextFrame` 活文字。
- 转曲文字、路径对象、图片内文字需要记录为人工处理风险。
- 版式溢出、缺字、旧文字残留必须在最终报告里说明。
