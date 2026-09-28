# course-kb · 课程知识库

[![Tests](https://github.com/BaymaxStudio/course-kb/actions/workflows/test.yml/badge.svg)](https://github.com/BaymaxStudio/course-kb/actions/workflows/test.yml) · [下载安装包](https://github.com/BaymaxStudio/course-kb/releases/latest) · [MIT License](LICENSE)

把课程 PDF、PPT、逐字稿和阅读材料整理成有出处的周索引与概念卡，再按需要导出复习包。公共发行版 **1.1.0**，数据结构 **1.1**。文件保存在你自己的电脑上。

## 安装

从 [GitHub Releases](https://github.com/BaymaxStudio/course-kb/releases/latest) 下载 `.skill` 或 `.zip`。两种格式包含同一套文件，发行页附 SHA-256 校验值。

需要能够使用本地技能、读文件和运行 Python 的 AI Agent，以及 Python 3.10 或更高版本。后端只用 Python 标准库，无需安装第三方 Python 包。PDF 阅读由 Agent 完成；录课转写可以使用飞书妙记等工具，后端不包含录音或转写服务。

- 支持导入 `.skill` 的 Agent：导入 `course-kb-public-1.1.0.skill`。
- 手动安装：解压同名 `.zip`，把完整的 `course-kb` 文件夹放进 Agent 的技能目录。例如 WorkBuddy 通常使用用户主目录下的 `.workbuddy/skills/`。文件夹内应直接看到 `SKILL.md`、`course_kb/` 和 `scripts/`，不要只复制 SKILL.md 或多套一层同名目录。
- 升级技能：先保留旧技能目录，再放入新目录，重新加载技能或开启新会话。**安装技能与迁移数据库是两个步骤**；安装不会自动修改已有课程资料。

GitHub CI 覆盖 macOS、Linux、Windows 的 Python 3.10 和 3.14，每组都运行自动化测试、构建和解压包验收。Windows 的文件锁与写入流程已在原生 Windows 托管运行环境通过检查；最新结果见顶部 Tests 链接。Windows 使用可用的 `py -3` 或 `python`，macOS/Linux 通常使用 `python3`。路径含空格或中文时加引号。知识库请放本机普通目录，网络盘与云同步目录的并发行为尚未验证。

## 装好后怎么开始

把学校的教学日历 PDF 交给 Agent，并说：

> 帮我建立这学期的课程知识库，这是学校的校历。然后把这份 PS101 第一周课件收进去。

Agent 会从校历提取周次和日期，创建知识库，再继续收材料。默认位置是 `~/course-kb`，也可指定其他目录。一个学期一个库；下学期另建目录。

校历暂时读不完整也能继续。未知值保留为待补信息，学校的 `w00`、特殊周和编号间隔原样保留；不会虚构日期。课程代码和课程名称需要有明确依据，老师、考核比例等可稍后补齐。

日常直接说：

> 这是 PS101 第三周的逐字稿，和已有 PPT 一起整理。

> PS101 哪几周还缺材料？哪些引文尚未核对？

> 导出 PS101 第三周到第五周的复习包。

复习包是生成文件，可带进另一个 AI 会话；本技能不负责授课，也不需要另装某个特定复习技能。

## 已有 1.0 知识库如何升级

对 Agent 说：

> 我已经有课程知识库，请先检查版本，再用迁移流程升级到 1.1。保留原件、笔记和现有定义，完成后校验并试导出一份复习包。

Agent 会先在库外生成候选改动，校验后应用；不会要求你重新上传全部课程材料。升级会增加材料登记、稳定来源编号、证据定位和核验状态，并修正导出的来源说明。

旧数据中的“原文引用”如果没有独立引文记录，会暂标为“未核验”，原定义文字保留。这表示仍需查原文，不代表内容必然错误。升级也不会自动补出缺失的课堂录音、逐字稿或页码。

写入时保留日志与旧版本。程序中断后由 Agent 执行 recover 恢复未完成批次；已经完成的升级不会被 recover 撤销。重要资料仍应有独立备份。

## 数据与可信度

```text
course-kb/                   # 这是数据目录，不是安装技能的目录
  manifest.json              # 学期、校历和课程清单
  _calendar.pdf              # 用户提供的校历，可暂缺
  _directory.md              # 自动生成的目录
  PS101/
    course.json              # 课程安排和考核
    raw/                     # 原件，只增不改
    sources.json             # 材料登记与来源性质
    index/weekly/            # 周索引
    index/concepts/          # 概念卡、出处与独立引文
    notes/                   # 自己的笔记，按需创建，Agent不擅自处理
  .course-kb/transactions/    # 更新日志与恢复备份
```

AI 纪要可以归档，但会标明其生成方式，不能导出成课堂逐字稿。摘要、原话、待核验引用分开记录，限制随复习包保留。校验通过只代表结构和引用关系符合规则，不代表每句话都已查证。

## 手动检查与维护

通常让 Agent 操作即可；这些命令供排错使用。把路径换成实际技能目录和数据库目录。Windows 将 `python3` 换成可用的 Python 命令。

```bash
python3 "/path/to/skill/scripts/validate.py" --root "/path/to/my-kb" --json
python3 "/path/to/skill/scripts/kb.py" migrate --root "/path/to/my-kb" --stage "/path/to/new-stage"
python3 "/path/to/skill/scripts/kb.py" apply --root "/path/to/my-kb" --stage "/path/to/new-stage"
python3 "/path/to/skill/gen_pack.py" PS101 --root "/path/to/my-kb" --out "/path/to/packs"
```

不要让其他工具同时直接编辑正式库的 JSON；绕过后端的工具不受锁和恢复机制保护。样例在 `examples/`，供理解字段和测试使用，不能当作自己的课程事实。版本变化见 `CHANGELOG.md`。

## 开发与贡献

克隆本仓库后，在仓库目录使用 Python 3.10+，无需第三方 Python 依赖：

```bash
python3 -m unittest discover -s tests -q
python3 scripts/build_public.py --out ../course-kb-release
python3 scripts/check_public.py ../course-kb-release/course-kb-public-1.1.0.skill --report ../course-kb-release-check.json
```

测试在临时目录使用虚构数据。`check_public.py` 验证解压后的安装包能从无关工作目录完成建库、入库、迁移和导出。CI 在 macOS、Linux、Windows 的 Python 3.10 和 3.14 环境运行同类检查；实际通过情况以工作流结果为准。

源码与分发版共用后端；`scripts/build_public.py` 通过固定文件清单生成可重复构建的安装包。修改数据规则时同步更新 `references/schemas.md` 与相关回归测试。

欢迎提交问题或 Pull Request。复现问题请使用最小虚构样例，提供 Python/系统版本和错误信息；不要上传课堂录音、他人逐字稿、私人笔记或访问凭据。材料的使用和分享权限由使用者自行确认。

代码采用 [MIT 许可证](LICENSE)。分发包不附带任何真实课程材料。
