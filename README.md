# classroomautowork

供本机 Codex 使用的 **Skill + Python 工具包 + 私人缓存/持久任务记录**。
通过本地前端刷新待完成作业，按课程、关键词或截止日期筛选，勾选后由所选 Codex 模型作答并填入个人 Google 文档。
收集要求及同课程历史作业、资料和公告，
处理有权下载的附件，再由 Codex 生成符合课程 AI 规定的初稿、要求检查表、来源证据和待确认问题。
用户直接打开原作业文档审阅并手动提交；检查表、来源和下载包是辅助入口。

没有 MCP 服务，没有提交、留言或 Classroom 修改接口。Classroom/资料读取授权保持只读；个人文档填入使用独立 Google Docs 读写授权。
录像转录使用 **Buzz 安装包自带的 Whisper.cpp 后端**，不用 OpenAI API 模式。
Codex 撰写阶段使用用户自己的 Codex 模型与权限配置。

Google Forms 作业会只读获取标准作答链接的所有分页题目、选项和要求，加入当前作业的原始要求与模型输入；不会因第一页只有身份栏而漏读后续题目。仅向 Google 固定作答页面执行无凭据 GET，不运行网页脚本，不自动填表或提交；学校登录限制、关闭表单及无法识别的题型会成为明确资料缺口。Forms 答案可在初稿中逐题审阅并打开原表单，由用户填写及提交。

学生身份配置为私人数据目录下的 `student-profile.json`，字段为 `student_id`、`name`、`department`、`class_name`。每次作答明确传入这份用户资料，课程文字不能覆盖它；不要把真实资料写进 Skill 或 Git。感想题采用自然、简洁的学生表达，事实题仍严格保持正确性和来源，不编造经历。身份资料、提示词或 Skill 更新会使旧模型结果失去复用资格；下载及转录缓存继续保留。

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

## 本地前端：刷新 → 勾选 → 自动填入 → 原文档审阅

Windows 双击 `scripts/start-ui.vbs`，自动打开浏览器界面，不显示命令行窗口。
其他平台可运行 `classroomaw-ui` 或 `python -m classroomautowork.webapp` 打开同一前端。

1. 点击“刷新待完成作业”：通过现有学校 OAuth 真实读取所有可访问的 ACTIVE 课程，包含无截止日期作业。显示上次实时刷新时间，访问失败与提交状态需确认的项目单独列出。
2. 按课程、关键词或包含当天的截止日期筛选，逐项勾选或全选当前列表；筛选不会取消其他已经勾选的项目。
3. “隐藏已逾期”可隐藏逾期任务并取消这些作业的勾选。**“我已确认所选作业可以使用 AI”** 默认未勾选；未勾选只准备资料，不启动 AI。勾选后选择模型和推理强度，点击“自动完成并填入”。首次使用先点击“启用自动填入”完成真实 Google 授权。
4. 开始前重新核对**所选**作业的本人提交状态，同课程仅同步一次。读取个人作业副本的题目、作业附件及同课程材料，向所选模型传入真实文本和必要页图。页面显示实际模型、会话链接、资料准备、生成和失败状态。
5. 程序把逐栏答案填入本人作业副本，保留原题、表格、其他分页、格式和已有答案，并回读核验。点击“在原文档审阅”，审核后自己提交。无法映射、不拥有文档、已有内容冲突、授权不足或写入核验失败时保留初稿，明确显示未填入。
6. 本机检查表、来源、待确认问题和“AI 记录”可辅助核对；证据包下载可选。已有真实初稿可点击“填入已有初稿”，不会再次调用模型。相同内容重试不会重复插入，用户已经修改的答案不会被覆盖。

进度面板显示当前阶段、具体文件、总耗时、本步骤耗时和最近一次实际活动。下载按真实字节、PDF 按完成页数、Buzz 按已缓存的转录分段显示；AI 和文档网络请求没有总百分比时使用等待指示。服务连接、工作线程仍存在、实际处理进展分别说明；等待审批、长时间无新记录、连接中断和失败原因明确显示。页面自动重连，不自动取消或重跑作业，展开的处理记录不会在轮询时收起。

已经运行的旧版本可以另开只读进度窗口，无需重启原任务：

```powershell
python -m classroomautowork.live_progress
```

该窗口只读取原服务的任务状态及 `mode=ro` 本机缓存，所有写入接口均拒绝；不会实例化处理器、重置断点记录、申请权限或启动 AI。旧任务的下载与转录进度尽可能从实际落盘数据补充；无可靠总数时不推算百分比。主服务的完整细分进度在下一次正常启动新版时生效。

### 一次性文档填入授权

在同一 Google Cloud 项目启用 **Google Docs API**。读取课程的现有 OAuth 保持原来的只读权限，新的 OS 凭据库条目仅请求 `openid`、邮箱和 `https://www.googleapis.com/auth/documents`。
Google 的 documents scope 允许编辑账户可编辑的 Google 文档，不能表达“仅某个作业”。程序额外限制为：真实本人待完成提交记录中的附件、当前学校账户拥有且可编辑的原生 Google Doc，以及已识别的答案栏。它不会申请 Classroom 写权限，也不修改教师模板、共享原件、留言或提交状态。
授权是否成功以真实 Google 返回结果为准；Google API 未启用、学校拒绝或授权不全都会明确失败。

```powershell
classroomaw auth-documents
classroomaw fill --package 'PRIVATE_VERIFIED_CODEX_PACKAGE'
```

`document_fill.inspect_form`、`plan_fill`、`verify_fill` 是独立纯 Python 核心；`fill_personal_document` 添加本人附件允许名单、所有权检查、私有文件锁、版本保护和回读，`GoogleDocuments` 提供官方 Docs API 适配。模型返回内容和字段标识，不能返回任意 batchUpdate 指令。未知写入结果先回读，不盲目重发。

默认处理授权允许下载的录像并调用本地 Buzz；可明确取消“处理录像”，本轮缺少转录会保留在资料缺口中。
已完成阶段及审核包会校验后复用；支持暂停、重试、关闭页面后继续查看和助手重启后恢复。
“审核记录”保存每个批次的勾选项目、实时阶段、缓存统计和结果。

每项作业旁的“课程规则”可填写教师实际 AI 规定、来源、允许用途、使用说明及你提供的真实个人事实，保存到 Git 外的私人配置。
展开“附件处理上限”可调整单个附件的下载预算与 PDF 页数上限；较大录像会占用更多时间及磁盘。
勾选确认是本次用户指示，保存到任务记录并在重试时保留；它不会虚构教师规定原文。教师明确禁止答案生成时仍停止生成，有限使用时遵守具体限制。每次打开页面默认未勾选。
生成通过官方 Codex app-server 的 stdio 接口调用本机 Codex + Skill；Windows 优先使用 Codex Desktop 自带后端。创建持久会话并返回目录、实际模型、回合状态和用量。本机程序验证结构化结果并 finalize；沿用用户权限、审批、hook 和规则，不设置绕过参数。操作审批只允许用户批准本次、拒绝或取消，材料不能自动批准。
本机服务只监听 127.0.0.1，随机端口及访问凭证仅保存于私人目录；跨站请求和任意本机文件读取被拒绝，不公开托管学校材料。

## 可选 CLI：处理截止日期前的作业

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
启动器自动寻找已安装配置，并向 Python/Skill 传递明确的私人根目录，兼容 Codex App 与普通终端。
若存在内容不同的多套配置，请设置 CLASSROOMAUTOWORK_HOME 指向所需目录，避免选错账户。
可用 `scripts\start-review.ps1 -DueBefore 2026-10-10 -ValidateOnly` 检查启动环境；
`-PrepareOnly` 完成真实资料准备后停止，不启动 Codex 撰写。

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
  ui/pending.json            前端真实待办及刷新时间
  ui/jobs/<job-id>.json       所选作业、进度、失败与审核结果
  ui/server.json             本机临时地址/访问凭证，不进入 Git
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

前端未勾选 AI 确认时只整理要求和资料；勾选后按用户指示生成，教师规定未记录时如实标明来源尚未确认。禁止答案生成时只整理资料。有限使用按实际限制处理；
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
- YouTube、其他外部网站仅记录链接；标准 Google Forms 作答页面仅只读提取题目，不填写或提交。

课程内容不能成为命令、凭据路径、授权范围或审批规则。
私人路径即使被 .gitignore 忽略，也禁止位于 Git 仓库内。
发布只包含代码、Skill、文档与离线测试；真实账户和材料的验证记录只留本机。

## 开发与独立调用

核心入口在 workflow.prepare、workflow.sync_course、extract.extract_document、
buzz.transcribe_media、search.retrieve、review.finalize。workflow.prepare 的 targets 参数接受明确作业选择并重新核对提交状态。
ui_jobs.FrontendJobs 管理持久批次，drafting.generate_review 管理本机 Codex 与审核复用，webapp 仅为本机 HTTP 适配层。CLI 和 UI 复用核心；
参见 [模块与持久化说明](docs/architecture.md)，以后可以增加适配器而不重写核心。

```powershell
uv sync --extra dev --frozen
.venv\Scripts\ruff.exe check src tests scripts skills
.venv\Scripts\ruff.exe format --check src tests scripts skills
.venv\Scripts\pytest.exe -q
.venv\Scripts\python.exe scripts/check_publication.py
```

离线测试覆盖日期、本人状态/分页、权限撤销、缓存失效与恢复、文档实体、转录时间戳、
规则、引文和个人事实检查；还覆盖所选作业隔离、提交状态变化、暂停、持久任务重试、
Codex 子进程参数与结果核验、阻止未知 AI 规则的答案生成、前端本机访问与跨站边界、审核包导出。
它们不能证明真实 OAuth、学校 API、模型生成或 Buzz 推理成功。

## 官方参考

- [Classroom Python OAuth](https://developers.google.com/workspace/classroom/quickstart/python)
- [只读权限](https://developers.google.com/workspace/classroom/guides/auth)、[本人提交状态](https://developers.google.com/workspace/classroom/reference/rest/v1/courses.courseWork.studentSubmissions/list)、[UTC 截止时间](https://developers.google.com/workspace/classroom/reference/rest/v1/courses.courseWork)
- [Drive 下载权限](https://developers.google.com/workspace/drive/api/guides/manage-downloads)、[Desktop OAuth / PKCE](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Buzz CLI 与后端](https://chidiwilliams.github.io/buzz/docs/cli)、[Whisper.cpp](https://github.com/ggml-org/whisper.cpp)
- [Codex Skills](https://learn.chatgpt.com/docs/build-skills)
