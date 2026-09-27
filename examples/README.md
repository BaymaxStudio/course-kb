# 字段示例

这些文件是虚构的输入模板，不是可直接使用的个人知识库。不要把示例课程、定义、时间戳写入用户数据。

- `manifest.json`：新建库模板。Agent 从用户校历填值，更新 `last_updated`，保留 `courses: []`；使用 `kb.py init --manifest ...`。附带校历时传 `--calendar-file`，脚本会记录实际复制路径。
- `course.json`：在 stage 添加课程；同时编辑 staged manifest 的 courses。`created` 使用实际当前时间；老师/总周数可以 null。
- `weekly.json`：放到 `index/weekly/w00.json` 的示例。实际周号必须采用学校校历。若日期未知，校历可声明 `"w00": null`，不要编造日期。
- `concept.json`：放到 `index/concepts/example-concept.json` 的示例。定义和context必须来自实际材料；本例为说明字段而虚构。没有定义则设 null 并省略 definition_basis。

示例 weekly 和 concept 暂不写 source_id：先在 stage 添加真实 raw 文件，再运行 prepare，后端自动登记并填入 ID。校验值只能由原件计算，不能复制占位值。之后检查 sources.json 的类别、生成方式与核验状态，必要时编辑并再次 prepare，才可 apply。

日常编辑已登记来源时保留其 ID。sources.json 的完整字段及引用规则见 `references/schemas.md`。
