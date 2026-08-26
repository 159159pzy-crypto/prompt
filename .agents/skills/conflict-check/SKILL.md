---
name: conflict-check
display_name: "互斥与质量自检"
description: "互斥与质量自检：视角/身份/服装/动作互斥表、细节标签过载、输出前自检清单（模板 §3/§3.1）。互斥 / 冲突 / 自检 / 质量"
---

互斥表（模板 §3.1，组装时逐项检查）：
视角互斥：from front × from behind；from above × from below；looking at viewer × facing away；pov × full body；close-up × full body。
身份互斥：solo × hetero/1boy/yuri；femdom × male-on-female rape；sleeping/unconscious × looking at viewer；blindfold × heart-shaped pupils/rolling eyes。
服装互斥：completely nude × 任何具体服装；pantyhose × barefoot（除非 torn pantyhose）；blindfold × glasses；内衣套装（cat lingerie、lace lingerie、babydoll、negligee、chemise 等）× no panties/bottomless（套装隐含内裤，需暴露时拆单件：cat bra + no panties）。不互斥：外衣/制服（maid outfit、school uniform、bunny suit、sailor uniform 等）与 no panties/bottomless 完全兼容。
动作互斥：standing sex × lying/on back；missionary × doggystyle；cowgirl position × prone bone；fellatio × cunnilingus（同一人执行）。
细节标签过度：同一身体部位细节标签 ≤2 且状态一致——spread toes × toe scrunch/toes curling/feet together；spread fingers × clenched fist/gripping；bouncing breasts × breasts squeeze together；open mouth × clenched teeth/closed mouth；rolling eyes × looking at viewer；spread legs × legs together；足部 ≥3 个标签（foot focus + footjob + toe scrunch + spread toes）过载。兼容例：barefoot + feet focus + soles + toe scrunch 可共存。例外：torn pantyhose + barefoot（脚部撕开）、partially undressed + 具体服装（半脱状态）合理。
输出前自检清单：① 人数一致性（无 1boy,2boys 矛盾）② 互斥冲突 ③ 重复标签（同标签不出现两次，强调靠位置不靠重复）④ 场景物理兼容（underwater × cigarette 不行）⑤ 灯光禁令（见 mood_library 的 §13.6）⑥ 标签总数在复杂度区间内（单人 16-30 / 双人 22-38 / 复杂 30-48）。返回 JSON 前删除重复 token、排除冲突、权重只在 0<值≤3 范围内。