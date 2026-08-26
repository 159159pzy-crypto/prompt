---
name: slot-order
display_name: "槽位与装配细则"
description: "槽位与装配细则：风格一致性铁律、每槽位数量、视线方向、自然语言短句、观众关系与多人规则（模板 §4）。槽位 / 装配 / 视线 / 多人"
---

装配细则（模板 §4）：
风格一致性铁律：clothing、scene、detail/mood 不能跨世界观矛盾——古风配古风（hanfu + ancient shrine + 水墨空灵）、赛博配赛博（latex bodysuit + cyberpunk city + 数字故障）、日常配日常（school uniform + classroom + 自然质感）。同世界观内不同场景混搭（kimono + love hotel）合理。
视线方向：单人未指定时注入 direct eye contact, facing viewer（放 expression 末尾或 camera 开头）；回头 turning around, direct eye contact；回眸 over shoulder, direct eye contact；背对/远去 from behind, facing away；侧脸 profile, from side；多人按互动（looking at another），不强制直视。
自然语言使用：tag 为主，仅在 tag 无法表达时用英文短句补充——角色间动作关系（one reaches toward the viewer while the other watches in silence）、复杂构图（girl sitting on boy's lap facing him）、特殊姿势组合、分镜对比（left panel: dressed, right panel: nude）。短句统一放所有 tag 之后（positive_tokens 末尾），一条短句解决一个歧义。
观众关系（叙事互动）：剧情场景必须用末尾短句描述角色与观众关系——邀请 as if inviting the viewer to escape together；审判 as if judging the viewer；托付 as if handing the last hope to the viewer；挑衅 as if daring the viewer to come closer；求助 as if begging the viewer for help；炫耀 as if showing off to the viewer what they can't have；羞耻 as if aware of being watched by the viewer；臣服 as if offering herself entirely to the viewer。
多人规则：每个角色必须补关键外观锚点（角色名 with 发色 + 瞳色 + 关键特征），动作/关系/剧情放末尾自然语言。结构=人数→角色A外观→角色B外观→共享tag（体位/镜头/场景）→末尾关系描述。错误示例 raiden shogun, long purple hair, playful, yae miko, pink hair, embarrassed, skirt lift（属性归属不明）；正确示例 2girls, raiden shogun with long purple hair and purple eyes, yae miko with long pink hair and fox ears, skirt lift, shrine, one playfully lifting the other's skirt with a mischievous smirk while the other looks shy and embarrassed。