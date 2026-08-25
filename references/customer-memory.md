# 本地长期客户档案

## 目录

- 同意与首次建档
- 客户槽位
- 六类记忆
- 写入与召回
- 用户控制
- 限制与隐私

## 同意与首次建档

运行：

```bash
python3 scripts/customer_memory.py status
```

只有用户明确同意后运行：

```bash
python3 scripts/customer_memory.py enable --confirm
```

首次建档优先保存用户明确提供的稳定字段：`industry`、`role`、`offering`、`typical_customer`、`average_deal_size`、`sales_cycle`、`territory`、`channels`、`common_decision_chain`、`main_challenge`。用户使用 `scope=user`、`subject_id=user`、`source_type=user_explicit`。

## 客户槽位

- 客户01至客户30：`client-01` 至 `client-30`
- 兼容旧称：客户A/B/C分别映射为 `client-01`、`client-02`、`client-03`
- 临时会话：不调用记忆脚本的写入命令

运行 `python3 scripts/customer_memory.py clients` 查看30个槽位的容量、已用数量、剩余数量和已有标签。一个槽位代表一个客户组织，不要混入另一家公司。

## 六类记忆

| scope | 内容 | 示例字段 |
| --- | --- | --- |
| `user` | 卖方稳定背景 | `industry`、`offering`、`territory` |
| `account` | 客户公司事实 | `display_label`、`industry`、`region`、`size`、`business_model`、`current_system` |
| `contact` | 关键联系人事实 | `contact.it-director.display_label`、`contact.it-director.role`、`contact.buyer.decision_role` |
| `deal` | 当前商机快照 | `deal.main.stage`、`deal.main.need`、`deal.pilot.timeline`、`deal.pilot.next_milestone` |
| `event` | 带时间的互动 | `meeting`、`commitment`、`objection`、`follow_up`、`stage_change` |
| `hypothesis` | 可纠正的判断 | `decision_weight`、`intent`、`risk`、`stakeholder_dynamic` |

组织、联系人和商机事实只接受用户陈述、转述或可靠公开来源。模型推断只能进入 `hypothesis`，必须带 `confidence` 和证据引用。一个客户有多位联系人或多个商机时，使用稳定、非敏感的字段命名空间，例如 `contact.it-director.public_role`、`deal.pilot.stage`；不要使用私人电话号码充当键。

## 写入与召回

示例：

```bash
python3 scripts/customer_memory.py apply --json '{"scope":"deal","subject_id":"client-01","field":"deal.main.stage","value":"已完成首次需求访谈，等待技术评估","source_type":"user_report","source_ref":"turn:当前任务","occurred_at":"2026-08-19","confidence":"high"}'
```

公开公司或职业信息：

```bash
python3 scripts/customer_memory.py apply --json '{"scope":"contact","subject_id":"client-01","field":"contact.buyer.public_role","value":"公开资料显示为采购负责人","source_type":"public_source","source_ref":"https://example.com/source","occurred_at":"2026-08-19","confidence":"high"}'
```

召回只加载当前客户：

```bash
python3 scripts/customer_memory.py context --subject-id client-01 --max-chars 5000
```

不要把召回内容再次写回。信息冲突时以用户最新纠正为准，并覆盖稳定字段；事件保留时间。决策权重随阶段变化时更新同一假设字段，不累积相互冲突的权重版本。

成功写入后只告诉用户：

> 档案更新：客户01的主商机阶段已更新为“等待技术评估”。撤销：告诉我“撤销刚才的档案更新”。

不得在回答中暴露数据库路径、内部 JSON 或无关客户资料。

## 用户控制

- 查看全部槽位：`clients`
- 查看当前客户：`show --subject-id client-01`
- 撤销最近一次：`undo`
- 暂停/恢复：`pause`、`resume`
- 永久删除客户：先确认槽位，再运行 `forget-client client-01 --confirm`
- 撤回同意但保留：`revoke --confirm`
- 撤回并删除：`revoke --delete --confirm`
- 清空全部：用户再次明确确认后运行 `clear --confirm`

## 限制与隐私

每条内容最多 400 字，总计最多 6,000 条；每客户公司、联系人、商机和事件各40条，假设10条。30个客户即使接近各自上限仍有容量余量。整段聊天、录音、会议全文、附件和截图继续留在原文件，只保存压缩后的高价值字段。

不得保存私人电话、私人邮箱、家庭住址、证件、金融账户、健康、宗教、族裔、性取向等敏感字段。公开人物也只保存与商业判断直接相关的公开职业信息。
