# Illustrator Manual Translator Skill

用于让支持 `SKILL.md` 目录的 agent 处理 Adobe Illustrator 说明书翻译工作流：

- 导出 `.ai` 文件里的 Illustrator `TextFrame` 活文字
- 生成翻译 JSON 模板
- 把译文写回 `.ai` 副本
- 导出 PDF
- 生成替换和验证报告

## 客户侧前提

- macOS
- Adobe Illustrator 已安装并有合法授权
- Python 3
- 终端或 agent 已被 macOS 允许控制 Adobe Illustrator
- 可选：Poppler，用于更完整的 PDF 验证

## 一条命令安装

公开 GitHub Release 发布后，客户执行：

```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_ORG/illustrator-manual-translator-skill/main/install.sh | bash
```

私有仓库或内网分发时，传入 zip 下载地址：

```bash
ILLUSTRATOR_MANUAL_TRANSLATOR_ZIP_URL="https://example.com/illustrator-manual-translator-skill.zip" \
  bash install.sh
```

默认安装到：

```text
${CODEX_HOME:-$HOME/.codex}/skills/illustrator-manual-translator
```

也可以指定目录：

```bash
bash install.sh "$HOME/.agents/skills"
```

## 发布方式

1. 确认 skill 目录有效：

```bash
python3 /path/to/quick_validate.py skills/illustrator-manual-translator
```

2. 生成 release asset：

```bash
./scripts/package.sh
```

输出：

```text
dist/illustrator-manual-translator-skill.zip
```

3. 在 GitHub Release 上传这个 zip。

## 安装后验证

```bash
python3 "$HOME/.codex/skills/illustrator-manual-translator/scripts/illustrator_manual_workflow.py" doctor
```

如果未安装 Adobe Illustrator，`doctor` 会失败。这是正常的环境前置条件失败，不是 skill 包损坏。

