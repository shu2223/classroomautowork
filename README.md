# classroomautowork

供本机 Codex 使用的 **Skill + Python 工具包 + 私人缓存/持久任务记录**。
按本地截止日期筛选各课程的待完成作业，收集要求及同课程历史作业、资料和公告，
处理有权下载的附件，再由 Codex 生成符合课程 AI 规定的初稿、要求检查表、来源证据和待确认问题。
生成本地审核包后停止，由用户审阅并手动提交。

没有 MCP 服务，没有提交、留言或 Classroom 修改接口。Google 权限固定为只读。
录像转录使用 **Buzz 安装包自带的 Whisper.cpp 后端**，不用 OpenAI API 模式。
Codex 撰写阶段使用用户自己的 Codex 模型与权限配置。

## 安装与真实连接

需要 Python 3.11+、操作系统安全凭据库；文档处理依赖由 uv.lock 固定。
录像还需要 Buzz、ffmpeg 和 ffprobe。本版已在 Windows 验证；其他平台需指定兼容 Buzz 安装路径。

```powershell
uv sync --extra dev --frozen
.venv\Scripts\classroomaw.exe doctor
.venv\Scripts\python.exe scripts/install_skill.py
```

安装器将 Skill 放入 $CODEX_HOME/skills/classroom-assistant（默认 ~/.codex/skills），
将解释器路径存入私人 runtime.json。启动新的 Codex 会话后可调用 $classroom-assistant。

在 Google Cloud 启用 Classroom API 和 Drive API，创建 **Desktop app** OAuth 客户端；
JSON 必须放在 Git 外。学校内部应用需要组织许可，External 测试应用需要添加测试用户。
学校管理员限制不能绕过。以下邮箱、路径和链接是待用户替换的配置占位符，不是验证结果。

```powershell
.venv\Scripts\classroomaw.exe init --client 'C:\private\client.json' --school-email 'YOUR_SCHOOL_EMAIL'
.venv\Scripts\classroomaw.exe auth
.venv\Scripts\classroomaw.exe pending --due-before 2026-10-10
.venv\Scripts\classroomaw.exe verify --assignment-url 'YOUR_REAL_ASSIGNMENT_DETAILS_URL'
```

OAuth 使用系统浏览器、回环回调和 PKCE，并核对 Google 返回的学校邮箱。
Token 只存入 OS 安全凭据库，拒绝明文备用存储。
只有 verify 对真实 API 读取作业、检查 canDownload、实际下载/导出并核对版本、大小及校验值后，
才写入私人 connection-receipt.json 的 verified 状态。失败会覆盖旧成功状态。
doctor、依赖安装和离线测试不能证明连接成功；prepare 在真实检查未通过时拒绝运行。
未指定验证作业时从真实待办选择一项；无附件时请指定带附件的真实作业。
Classroom 待办总览链接不是作业详情 ID。

## 一次启动处理截止日期前的作业

双击 scripts/start-review.cmd，输入包含当天的截止日期。
启动器先运行只读资料准备，再调用本机 codex exec 读取缓存并生成审核包。
它沿用用户的模型、沙箱、审批、hook 和规则，不设置绕过参数。
没有 Codex CLI 时只准备资料，随后在 Codex App 中调用 Skill 继续。

```powershell
scripts\start-review.ps1 -DueBefore 2026-10-10
# 仅在明确需要时包含无截止日期作业，或暂缓录像：
scripts\start-review.ps1 -DueBefore 2026-10-10 -IncludeNoDue -DeferMedia
```

也可以在 Codex 中说：

> 使用 $classroom-assistant，处理 2026-10-10 截止前的待完成作业，生成供我审核的初稿和审核包。

默认时区 Asia/Tokyo；API 的 UTC 截止时间先转换。
不自动处理已提交、已退回或状态不明的任务，后两种列为待确认。
部分课程访问失败、超预算附件和未处理录像会明确列出，不宣称完整。

## Buzz 本地后端

```powershell
.venv\Scripts\classroomaw.exe buzz-setup --buzz-root 'C:\Program Files (x86)\Buzz' --model-size small --language ja
.venv\Scripts\classroomaw.exe buzz-status
```

从 Whisper.cpp 上游指定模型仓库下载多语种模型，使用上游 LFS SHA-256 核验，
记录 Buzz 自带引擎和模型的校验值；不上传课程内容。
tiny/base/small/medium/large-v3 可选。tiny 适合快速验证；日语讲课可用较大模型并核对错误。
配置完成不等于推理验证完成。实际转录输出 SRT、VTT、带原录像绝对时间的片段 JSON。
录像按五分钟拆分成独立持久任务，失败后重跑复用已完成片段。

## 私人配置与课程规则

默认状态根目录为 %LOCALAPPDATA%\classroomautowork（Linux 使用 XDG 数据目录）。
settings.json 的 data_dir 可指向其他 Git 外目录；全局 --config 可选择另一私人设置。
Windows App 的环境变量可能指向 LocalCache，实际路径以 init 输出和 settings.json 为准。

```text
state root/
  settings.json             学校邮箱、客户端路径、时区、data_dir
  runtime.json              Skill 使用的 Python 路径
  buzz.json / models/        本地转录配置和模型
data_dir/
  connection-receipt.json    真实连接记录（私人）
  courses/<course-id>.json   用户维护的课程规则与预算
  tasks.sqlite              重试状态、尝试次数、缓存和检索索引
  snapshots/ attachments/ processed/
  review-packages/<course>/<assignment>/<revision>/
  latest-selection.json / latest-batch.json
```

首次同步生成默认课程配置，ai_use 为 unknown。用户按真实课程规定填写：

| 字段 | 含义 |
| --- | --- |
| ai_use | unknown / forbidden / limited / allowed |
| policy_evidence | 规定原文、文件/页码/公告来源；已知规则必填 |
| limitations | limited 时允许的具体用途，必填 |
| disclosure | 课程要求的 AI 使用说明 |
| personal_facts | 用户真实经历、出席、调查或记录；默认空数组 |
| max_attachment_bytes | 默认 512 MiB，超预算保留为待处理，提高后重试 |
| max_pdf_pages / render_dpi | 默认 1000 页 / 110 DPI |
| language | 默认 ja |

未知或禁止答案生成时只生成要求检查和待确认问题。有限使用按实际限制处理；
更严格的作业规则优先。Codex 不能擅自改成允许，也不能编造个人经历、材料或引用。

## CLI 与审核结果

```powershell
.venv\Scripts\classroomaw.exe prepare --due-before 2026-10-10
.venv\Scripts\classroomaw.exe prepare --assignment-url 'YOUR_REAL_ASSIGNMENT_DETAILS_URL'
.venv\Scripts\classroomaw.exe search --course-id COURSE_ID --query '关键词'
.venv\Scripts\classroomaw.exe prepare --assignment-url 'YOUR_REAL_ASSIGNMENT_DETAILS_URL' --evidence-query '补充关键词'
.venv\Scripts\classroomaw.exe tasks
.venv\Scripts\classroomaw.exe finalize --package 'PRIVATE_PACKAGE_DIR' --review 'PRIVATE_REVIEW_JSON' --draft 'PRIVATE_DRAFT_MD'
```

prepare 只完成资料包，不把空模板称为初稿。Skill 阅读真实资料后写 draft.md 和 review.json，再 finalize。
审核包包含原要求、真实证据 ID 与页码/段落/时间戳、要求检查表、待确认问题和资料缺口。
答案被规则阻止时省略 --draft。引文必须逐字匹配来源；个人事实必须对应用户提供的事实索引。
证据被修改、已不在最新可读索引或规则变化时，要求重新准备。
语义正确性和要求是否完整仍由 Codex 与用户检查，程序校验不代替人工审阅。

## 缓存、恢复与格式边界

- 内容版本忽略轮换的缩略图链接；正文变化才重新记录。
- Drive 按真实版本缓存，处理按内容 SHA-256、处理器版本与配置缓存。每次同步先重新检查权限。
- 失败保存状态，下一次重试；进程退出后可恢复。单目录文件锁防止重复处理。
- 普通文件下载保留版本校验的 Range 断点；Google 原生文档中断后重新导出。
- PDF 提取文字并保留全部页面预览；低文字量页面标记为需目视核对，不冒充 OCR 成功。
- DOCX/PPTX 提取段落/幻灯片文字，不运行宏、外链或 XML 实体；保留原文件。TXT/MD/CSV 按行定位。
- Google Docs/Slides/Sheets 导出 PDF。旧 DOC/PPT/XLS、复杂布局和不支持格式保留为需人工处理。
- canDownload 为 false 时不下载，也不把旧附件缓存用于当前检索。快捷方式和不支持的原生文件显示缺口。
- YouTube、外部网站、Forms 仅记录链接，不自动下载、填写或提交。

课程内容不能成为命令、凭据路径、授权范围或审批规则。
私人路径即使被 .gitignore 忽略，也禁止位于 Git 仓库内。
发布只包含代码、Skill、文档与离线测试；真实账户和材料的验证记录只留本机。

## 开发与独立调用

核心入口在 workflow.prepare、workflow.sync_course、extract.extract_document、
buzz.transcribe_media、search.retrieve、review.finalize。CLI 是薄适配层；
参见 [模块与持久化说明](docs/architecture.md)，以后可以增加适配器而不重写核心。

```powershell
uv sync --extra dev --frozen
.venv\Scripts\ruff.exe check src tests scripts skills
.venv\Scripts\ruff.exe format --check src tests scripts skills
.venv\Scripts\pytest.exe -q
.venv\Scripts\python.exe scripts/check_publication.py
```

离线测试覆盖日期、本人状态/分页、权限撤销、缓存失效与恢复、文档实体、转录时间戳、
规则、引文和个人事实检查。它们不能证明真实 OAuth、学校 API 或 Buzz 推理成功。

## 官方参考

- [Classroom Python OAuth](https://developers.google.com/workspace/classroom/quickstart/python)
- [只读权限](https://developers.google.com/workspace/classroom/guides/auth)、[本人提交状态](https://developers.google.com/workspace/classroom/reference/rest/v1/courses.courseWork.studentSubmissions/list)、[UTC 截止时间](https://developers.google.com/workspace/classroom/reference/rest/v1/courses.courseWork)
- [Drive 下载权限](https://developers.google.com/workspace/drive/api/guides/manage-downloads)、[Desktop OAuth / PKCE](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Buzz CLI 与后端](https://chidiwilliams.github.io/buzz/docs/cli)、[Whisper.cpp](https://github.com/ggml-org/whisper.cpp)
- [Codex Skills](https://learn.chatgpt.com/docs/build-skills)
