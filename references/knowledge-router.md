# 本地混合 RAG 知识库路由

本地语料库包含开放营销/心理/管理教材、公版销售与沟通经典、GitHub 开源销售/GTM/客户成功 Skill、狗头军师沟通材料和图片 OCR。它用于补充框架与案例，不替代当前客户证据。

## 检索预算

- 普通问题：3 个片段，总计 3,000–5,000 字符，使用 `--top-k 3 --max-chars 5000`。
- 复杂成交分析：只有用户需要完整销售判断或多阶段成交方案时，整轮累计最多6个片段、8,000–10,000字符。通常分3个主题查询，每次用 `--top-k 2 --max-chars 3400`，合并去重后裁到10,000字符；若只查一次，使用 `--top-k 6 --max-chars 10000`。
- 只取高相关、互补的片段；结果不足时不用低相关内容凑到下限。

## 检索命令

先把同一销售意图压缩为中文和英文两条检索语句，并识别销售阶段与媒介。两条语句只能翻译和补充同一意图，不得偷偷加入未经用户提供的客户事实。从 Skill 目录运行：

```bash
python3 scripts/hybrid_search.py \
  --query "制造企业预算冻结，是时机问题还是项目价值不足" \
  --query-zh "企业采购 预算冻结 项目优先级 决策链 异议验证" \
  --query-en "B2B budget freeze project priority buying committee objection validation" \
  --stage negotiation --channel online --top-k 3 --max-chars 5000
```

混合检索并行使用 FTS5 词干/子串检索和中英文 BGE 向量，再按相关性、销售阶段、媒介、来源等级和多样性重排。脚本默认使用普通预算；复杂成交时显式传入 6/10000。

可用 `--include` 限定来源路径、标题或知识域。混合索引默认位于 `~/Documents/ChatGPT/销冠/资料库/混合索引`；迁移后设置 `XIAOGUAN_HYBRID_INDEX`。

`--intent` 路由：通常销售推进用 `sales`；获客投放与内容增长用 `marketing`；认知与组织行为机制用 `psychology`；续约、健康度和增购用 `customer_success`。不明确才用 `general`。意图只调整来源优先级，不能替代事实核验。

如果脚本返回 `lexical_fallback`、索引缺失或本地向量运行时不可用，再运行旧关键词检索作为降级，不得假装使用了向量：

```bash
python3 scripts/search_corpus.py --query "B2B discovery 决策链 需求访谈" --top-k 3 --max-chars 5000
```

## 常用检索词

| 任务 | 建议组合 |
| --- | --- |
| 客户画像/决策链 | `buyer persona stakeholder decision MEDDICC economic buyer` |
| 需求发现 | `discovery SPIN pain impact qualification` |
| 线上触达 | `cold email outreach sequence LinkedIn reply` |
| 电话/会议 | `cold call meeting agenda objection` |
| 主导权/控场 | `straight line sales call control agenda certainty tonality loop objection` |
| 销售者与产品确信 | `salesperson credibility authority product belief proof certainty trust` |
| 欲望放大 | `desire motivation outcome visualization emotional benefit value amplification` |
| 紧迫性 | `authentic urgency scarcity price window opportunity cost deadline` |
| 异议循环 | `objection handling isolate objection clarify respond confirm re-close` |
| 明确成交 | `closing ask for the sale trial close commitment next step close` |
| Demo/方案 | `demo proposal business case ROI` |
| 谈判/报价 | `negotiation pricing concession BATNA procurement` |
| 客户成功 | `onboarding health score QBR renewal expansion churn` |
| 心理与行为 | `social influence motivation cognitive bias organizational behavior` |
| 情绪和沟通 | `情绪价值 接话 冲突 拒绝 表达逻辑` |

## 使用规则

1. 先用客户事实定位问题，再检索；不要用检索结果反向伪造客户事实。
2. 检查返的 `mode`、`score_components`、`evidence_grade`、阶段和媒介标签，在当前预算内只留互补分片。
3. 第一手材料用于事实核验；教材和研究解释机制；开源 Skill 用于流程和模板；公版经典只作历史启发。
4. 不把检索结果整段倾倒给用户，只转化为用户所问问题的判断和动作。来源彼此重复时只保留一项；用户主动问知识来源时再说明实际调用内容。
5. 本地资料缺少当前行业、地域或人物信息时，联网搜索官方和近期来源；时效敏感信息必须核验。
6. 多个框架冲突时，以客户证据、阶段适配、客户利益和低风险试验为准。
7. 用户只问画像、异议、跟进、报价、复盘或某个具体问题时，只检索该问题，不擅自扩展为完整销售方案。
8. 用户明确需要完整成交策略时，不把全部目标塞进一条宽查询。按 `客户心理/需求/决策权重`、`销售者与产品确信/欲望`、`紧迫性/异议/成交动作` 三个主题检索，各取1–2片；全部查询合计仍不得超过6片/10,000字符。
9. 检查具体文本是否直接回答当前问题。即使总分尚可，若阶段或媒介明显错位、语义分为零、只有通用模板或需要大量牵强解释，也应弃用。
