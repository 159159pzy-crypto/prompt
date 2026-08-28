---
name: clothing-library
display_name: "服装与状态标签库"
description: "服装与状态：类型、材质、穿着状态、改造维度。"
triggers:
  - 服装
  - 衣服
  - 制服
  - 裙
  - 内衣
  - 裸体
  - 穿着
  - 丝袜
  - outfit
  - outfits
  - clothing
  - uniform
  - dress
sections: [types, remodel]
---

槽位 [clothing/state]，公式：原服装 × 改造方向。选 2-10 个。

光谱：正常 → 滑落/露出 → 掀起/敞开 → 半脱 → 仅剩配饰（completely nude + 单件配件）→ 破损/湿透。
改造叠 1-3 维：透明化 / 裁剪 / 镂空 / 破损 / 胶衣 / 裸+配饰 / 非对称。
反差：最高正经度服装 × 最高暴露度改造（school uniform + micro skirt + no panties）。
词表：`read_skill` section=`types` 或 `remodel`。
