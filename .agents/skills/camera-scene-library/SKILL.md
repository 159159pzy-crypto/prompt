---
name: camera-scene-library
display_name: "镜头与场景标签库"
description: "镜头与场景：景别、视角、POV、场所、天气。"
triggers:
  - 镜头
  - 构图
  - 室内
  - 街头
  - 卧室
  - 教室
  - 办公室
  - 摄影
  - 视角
  - camera
  - composition
  - bedroom
  - classroom
sections: [shots, places]
---

槽位 [camera/shot] 1-5 个，[scene/environment] 2-6 个。POV 不能与 full body 同时用。光线/光影/色调禁词以 banlist.py 为准；允许天气与时辰。不要输出 backlit / chiaroscuro / dramatic shadows。镜头光学允许 lens flare、bloom。词表：`read_skill` section=`shots` 或 `places`。
