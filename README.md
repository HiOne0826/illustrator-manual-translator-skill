# Illustrator Manual Translator Skill

用于让支持 `SKILL.md` 目录的 agent 通过文件和对话处理 Adobe Illustrator 多语种说明书：

- 规格书拆解后生成一份 `说明书内容确认.xlsx`
- 用户先校准黄色的“最终中文”列并在对话中确认
- 在同一 Excel 追加多语种翻译，用户校准“最终译文”后再确认
- 不使用 `action`、`review_status` 或逐行审批字段
- 确认后自动写入 `.ai` 副本、导出 PDF 并执行版式校对
- 中文和每个目标语种先生成电子版，再生成完整可编辑 AB AI/PDF
- 用户确认全部 AB 版后才拆分可编辑 A/B AI/PDF，最后形成交付目录
- 自动校验缺页、重页、左右顺序、A/B 合集、页面尺寸、出血和画板数量
- 可把 4 画板 A4 说明书原生重排为双面五折页：每面 5 个面板、保留可编辑对象，复刻参考模板的 10 个 guides，并按完整章节填充额外面板；章节整体顶对齐，标题、正文和图片的原始相对间距不变，内容不足时保留自然底部留白
- 五折页默认模板精确复现参考尺寸 `390 × 174.6 mm`；打印长边/短边翻转方向必须由客户或印厂确认

## 客户侧前提

- macOS
- Adobe Illustrator 已安装并有合法授权
- Python 3
- 支持工作区文档能力的 Codex 运行时（提供 `@oai/artifact-tool`）
- 终端或 agent 已被 macOS 允许控制 Adobe Illustrator
- Poppler（至少提供 `pdftoppm`），用于生成逐页 PDF 校对图

## 一条命令安装

公开 GitHub Release 发布后，客户执行：

```bash
curl -fsSL https://raw.githubusercontent.com/HiOne0826/illustrator-manual-translator-skill/main/install.sh | bash
```

私有仓库或内网分发时，传入 zip 下载地址：

```bash
ILLUSTRATOR_MANUAL_TRANSLATOR_ZIP_URL="https://example.com/illustrator-manual-translator-skill.zip" \
  bash install.sh
```

如果继续使用私有 GitHub 仓库，需要给 `curl` 和安装脚本都传 GitHub token：

```bash
export GITHUB_TOKEN="github_pat_xxx"
curl -fsSL \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://raw.githubusercontent.com/HiOne0826/illustrator-manual-translator-skill/main/install.sh \
  | GITHUB_TOKEN="$GITHUB_TOKEN" bash
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
python3 "$HOME/.codex/skills/illustrator-manual-translator/scripts/manual_workflow.py" doctor
```

如果未安装 Adobe Illustrator，`doctor` 会失败。这是正常的环境前置条件失败，不是 skill 包损坏。
