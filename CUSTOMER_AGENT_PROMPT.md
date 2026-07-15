# 客户 Agent 使用提示词

请安装并使用 `illustrator-manual-translator` skill。

```bash
curl -fsSL https://raw.githubusercontent.com/HiOne0826/illustrator-manual-translator-skill/main/install.sh | bash
```

安装完成后，请使用这个 Skill 处理我提供的产品规格书和 Illustrator 说明书模板：

1. 抽取规格书与模板字段，生成 `说明书内容确认.xlsx`。
2. 把 Excel 发给我；我只修改黄色的“最终中文”列。
3. 在我明确回复“中文内容已确认”以前，不要进入翻译。
4. 在同一个 Excel 中追加多语种翻译；我只修改黄色的“最终译文”列。
5. 在我明确回复“翻译内容已确认”以前，不要进入排版。
6. 调用 Illustrator 生成各语种 AI、PDF 和预览，执行溢出、版式、旧文字残留等校对。
7. 把预览给我看；在我明确确认版式后，才整理最终交付文件。

Excel 中不要出现 `action`、`review_status`、通过、驳回或逐行审批字段。用户确认以对话中的整表确认消息为准。

始终复制源 `.ai` 后再处理，不得覆盖源文件。转曲文字、图片内文字、缺失模板映射或无法自动排版的区域必须明确报告。
