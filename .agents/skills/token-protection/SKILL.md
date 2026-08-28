---
name: token-protection
display_name: "保护词"
description: "保护词：LoRA、Embedding、BREAK、触发词和显式权重逐字保留。"
triggers: []
---

LoRA（`<lora:...>`）、Embedding（`<embed:...>`）、BREAK、用户给出的触发词、显式权重 `(tag:数值)` 必须逐字保留：不翻译、不归一化、不拆分、不重排。include_chinese 时这些项的译文也保持原文。
