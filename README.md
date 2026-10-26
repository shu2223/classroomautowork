# classroomautowork

供本机 Codex 使用的 **Skill + Python 工具包 + 私人缓存/持久任务记录**。
按截止日期筛选待完成作业，点击“一键完成”，由所选 Codex 模型读取资料、作答并自动填写。已有答案集中在“待审阅”，可直接打开个人文档、原表单或 Word。
收集要求及同课程历史作业、资料和公告，
处理有权下载的附件，再由 Codex 生成符合课程 AI 规定的初稿、要求检查表、来源证据和待确认问题。
用户直接打开原作业文档审阅并手动提交；检查表、来源和下载包是辅助入口。

没有 MCP 服务，没有提交、留言或 Classroom 写入 API。Classroom/资料读取授权保持只读；个人文档填入使用独立 Google Docs 读写授权。当前作业要求创建本人ドキュメントシート时，可通过学校登录浏览器的“添加或创建→文档”创建并核验本人附件；不修改教师内容、其他作业、权限或提交状态。
录像转录固定使用 **Buzz 安装包自带的 Whisper.cpp 后端和 Whisper 大模型**，默认 large-v3-turbo，不用 OpenAI API 模式。
Codex 撰写阶段使用用户自己的 Codex 模型与权限配置。

Google Forms 作业会只读获取标准作答链接的所有分页题目、选项和要求，加入当前作业的原始要求与模型输入；不会因第一页只有身份栏而漏读后续题目。仅向 Google 固定作答页面执行无凭据 GET，不运行网页脚本，题目读取不执行填写；学校登录限制、关闭表单及无法识别的题型会成为明确资料缺口。模型返回经过题目摘要、真实 entry ID 和原选项校验的逐栏答案，Python 通过 Google 原生预填链接自动打开并填写原表单，用户在原页面审阅后手动提交。不会调用 formResponse POST 或点击提交。浏览器打开记录与页面字段回读、Google 草稿保存分别记录，不能把链接生成冒充网页核验。重复处理复用答案且不自动再次打开覆盖用户已修改的草稿；预填入口会重新带入候选答案，已修改过的草稿可从原始链接继续。

学生身份配置为私人数据目录下的 `student-profile.json`，字段为 `student_id`、`name`、`department`、`class_name`。每次作答明确传入这份用户资料，课程文字不能覆盖它；不要把真实资料写进 Skill 或 Git。感想题采用自然、简洁的学生表达，事实题仍严格保持正确性和来源，不编造经历。身份资料、提示词或 Skill 更新会使旧模型结果失去复用资格；下载及转录缓存继续保留。

表单生成会逐一核对必填栏，漏掉原生栏位时使用同一模型自动补全一次。仍缺答案或等待本人事实的表单显示“填写未完成”，保留已有答案和具体缺口，重试不会把旧预填链接当作全部完成。普通选填感想也生成候选回答；仅涉及真实个人情况且缺少资料的栏位保留为空。

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

## 本地前端：筛选 → 一键完成 → 打开审阅

Windows 双击 `scripts/start-ui.vbs`，自动打开浏览器界面，不显示命令行窗口。
其他平台可运行 `classroomaw-ui` 或 `python -m classroomautowork.webapp` 打开同一前端。

1. 首页先显示本机待办，首次使用或列表超过 15 分钟时自动刷新真实 Classroom 状态；页面保持打开或切回窗口时也会在空闲期间检查更新。也可手动点击“刷新作业”。
2. 按截止日期、课程或关键词筛选，也可隐藏逾期任务。不勾选时，“一键完成”处理**当前可见范围**；勾选后只处理选中项。筛选隐藏的项会取消勾选，已有交付结果自动跳过。
3. 默认使用 AI；模型、推理强度、大模型转录和文档授权收在“设置”，选择自动保存。关闭 AI 时按钮明确变为“仅整理资料”。
4. 同课程共用一次资料同步，先完成该课程的答案再处理下一课程，不再等待全部课程资料就绪。程序仍重新核对所选作业的本人提交状态、下载权限及材料版本。
5. 结果陆续进入“待审阅”，直接打开已填文档、原表单或 Word。来源、检查表、可选调整、模型会话和证据包在“详情”；完整日志在“处理记录”，按需展开。
6. 重试仅处理未完成项。仅填写失败时复用已生成答案，交由填写模块重新核对权限和内容；不重新下载整门课或调用模型。用户修改过的文档不会被覆盖，提交始终由你手动完成。

页面顶部提供“完成后自动关机”开关，默认关闭，只对当前或下一批作业生效。开启后，整批作业全部成功才进入 60 秒倒计时，可随时关闭开关或点击取消；暂停、失败、漏答、新任务或退出助手都会取消关机。关闭网页不影响后台等待，重新打开会显示当前开关和倒计时；重启助手后不会自动恢复开启。刷新、授权、旧完成记录和仅整理资料不会触发关机。目前支持 Windows 正常关机，不强制关闭应用。

已有旧后台仍在处理时，可运行 `python -m classroomautowork.power_service` 打开带开关的兼容界面，继续使用原任务、模型和缓存，不重启工作线程；关机设置只在本机内存中生效。

首页保留所有已同步但尚未提交的作业，已有答案标为“答案已生成 · Classroom 尚未提交”，可直接打开；一键完成仍跳过这些项目。筛选提示显示同步总数、当前显示数和已有答案数，避免把生成答案误当成提交完成。填写失败会直接显示具体原因；缺少文档映射时重新识别并生成映射，有有效映射时主按钮也只重试填写。个人文档支持段落中的文字勾选框、括号理由栏、关键词行及明确书面回答提示，保留原题并逐栏回读验证。

原文档只有在已识别的必答栏均已填入、回读一致且没有未识别题目时，才显示填写完成。人数与理由题、编号选项及其理由分别核对；完整初稿或部分栏位填写成功不能算原文档完成。漏答显示“填写未完成”并保留重试，重试先补全题目映射；已有答案、未选选项和身份审阅提示不会被覆盖或当成漏答。

同一次处理中的大模型及后端 SHA-256 校验可复用；文件签名变化立即重新校验，新一轮任务也重新校验。转录仍强制使用真实 Whisper 大模型，已完成片段从持久缓存读取。历史记录按需渲染，轮询不再重复构建全部历史详情。每项作业的最新结果独立于最近 30 条日志保留，频繁刷新不会使已有答案消失或重新进入一键处理范围。

进度面板分别显示实测步骤进度和估计总进度，并列出当前文件、总耗时、本步骤耗时、处理速度及最近一次实际活动。下载使用实际字节，PDF 使用完成页数；Buzz 读取 ffmpeg 的实际音频提取进度及 Whisper.cpp 的识别进度，按音频秒数显示文件内进度，并保留已缓存分段数。音频准备与语音识别分别计速，转录速度显示为实时播放的倍率；短尾段使用实际音轨长度。

剩余时间综合本次速率的平滑变化、同配置历史速率、每段启动开销及后续附件、AI 作答和填写的历史耗时，显示预计范围及日本时间的预计完成时刻。成功测量保存在私人数据目录的 `ui/progress-history.json`，仅含配置指纹和数值；断点已有进度、缓存命中、失败和人工审批等待不会被当成高速处理样本。首次测量不足、尚有未发现资料或未知后续阶段时明确显示校准或待估计；总进度标为估计，AI 不显示虚构的实测百分比。实际速度变慢、估计超时或长时间没有进展时重新估算，不把旧数字自行倒数到零。服务连接、工作线程、实际进展分别显示；暂停、审批和断线时冻结预计完成时刻。页面自动重连，展开的处理记录保持不变。

旧后台尚未返回估算字段时，刷新网页即可使用当前录像的真实分段日志估算，无需重启正在运行的任务。只有同时记录开始、实际推理和完成时间的至少两段参与计算，缓存瞬间命中不算速度样本。页面区分当前录像、可估计工作的时间小计和整批剩余时间；整批工作量未确认时显示已处理作业项数及具体原因，不把录像剩余时间冒充整批剩余时间。

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

原文档中的明确姓名、学号栏使用已保存的私人学生资料补填，并与答案一起回读核验。身份栏不接受模型指定的值，已有不同内容会保留；缺少资料或内容冲突会显示具体提示。界面按实际回读结果列出勾选、理由、关键词、文字答案及身份栏数量。

默认处理授权允许下载的录像并调用本地 Buzz；可明确取消“处理录像”，本轮缺少转录会保留在资料缺口中。
已完成阶段及审核包会校验后复用；支持暂停、重试、关闭页面后继续查看和助手重启后恢复。

上传报告类作业没有原生 Docs/Forms 答案栏时，程序自动生成 `answer.docx` 与本机阅读版。任务卡直接提供“打开答案”和“下载 Word”，预览默认显示答案。旧的已核验初稿也可直接导出，不重新下载资料或调用 AI；证据 ZIP 保持可选。未完成的原题、自评或个人事实明确显示待补充，Word 是审阅稿，不把模型回合完成视为全部作业已经完成。修改过的初稿或过期的核验结果不能继续下载为已核验答案。
“审核记录”保存每个批次的勾选项目、实时阶段、缓存统计和结果。

每项作业旁的“课程规则”可填写教师实际 AI 规定、来源、允许用途、使用说明及你提供的真实个人事实，保存到 Git 外的私人配置。
展开“附件处理上限”可调整单个附件的下载预算与 PDF 页数上限；较大录像会占用更多时间及磁盘。
勾选确认是本次用户指示，保存到任务记录并在重试时保留；它不会虚构教师规定原文。按用户要求，课程 AI 禁止或限制只保留为说明，不阻止已勾选的生成；不虚构教师许可。默认勾选并记住用户选择；主动取消后只整理资料，不启动 AI。
生成通过官方 Codex app-server 的 stdio 接口调用本机 Codex + Skill；Windows 优先使用 Codex Desktop 自带后端。创建持久会话并返回目录、实际模型、回合状态和用量。本机程序验证结构化结果并 finalize；沿用用户权限、审批、hook 和规则，不设置绕过参数。操作审批只允许用户批准本次、拒绝或取消，材料不能自动批准。
本机服务只监听 127.0.0.1，随机端口及访问凭证仅保存于私人目录；跨站请求和任意本机文件读取被拒绝，不公开托管学校材料。

## 可选 CLI：处理截止日期前的作业

双击 scripts/start-review.cmd，输入包含当天的截止日期。
启动器先运行只读资料准备，再调用本机 codex exec 读取缓存并生成审核包。
它沿用用户的模型、沙箱、审批、hook 和规则，不设置绕过参数。
AI 结果仅在真实回合成功结束且通过核验后用于填写。新回合最多运行 15 分钟（操作审批等待不计入总时限）、连续输出最多 5 分钟、输出最多 64,000 字符；超限停止并保留私人 `codex-output.partial.txt` 诊断片段，片段不能作为完成的答案。程序压缩重复索引并优先提供完整原题，完整课程资料仍保留在私人包中供按题检索。界面遇到长期输出会显示异常等待提示，不因活动计数增加就宣称答案能正常完成。后台代码更新在下次启动后生效，不中途取消既有回合。

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
.venv\Scripts\classroomaw.exe buzz-setup --buzz-root 'C:\Program Files (x86)\Buzz' --model-size large-v3-turbo --language ja
.venv\Scripts\classroomaw.exe buzz-status
```

从 Whisper.cpp 上游指定模型仓库下载多语种模型，使用上游 LFS SHA-256 核验，
记录 Buzz 自带引擎和模型的校验值；不上传课程内容。
只允许 large-v3-turbo（默认）或 large-v3；核验真实 GGML 编码器尺寸，不能将小模型改名绕过。模型变更只使旧转写失效，已完整下载的附件继续复用；首次切换会重新转录，之后按模型校验值和录像校验值复用。
配置完成不等于推理验证完成。实际转录输出 SRT、VTT、带原录像绝对时间的片段 JSON。
录像按五分钟拆分成独立持久任务，失败后重跑复用已完成片段。

## 一键交付与审阅

点击生成后先读取原题和同课程资料，再用所选 Codex 模型生成候选答案，自动填入个人 Google 文档或打开原生预填表单。AI 空答案、占位提示或格式错误自动修复一次，保持同一模型、权限及缓存输入，不重新下载附件。所有有效初稿同时生成可打开的 Word 文件；原页面填入遇到权限、版本或映射冲突时仍能审阅本机答案。

缺口和未确认事项只作为审阅提示，不将已有答案退回“需要补充”；重试直接自动续跑。“调整答案或说明（可选）”保留用户修正入口。使用可信学生身份和自然的学生语气，普通感想及生活情境直接给出完整候选模板；未知实际个人数值、出席或调查数据不编造，相应原栏保留为空，其他答案继续交付。原题未读取或来源无权获取仍如实标记，不能保证技术失败时一定产出正确完整答案，不把本机文件存在冒充已填入或已提交。

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

前端未勾选 AI 确认时只整理要求和资料；勾选后按用户指示生成，教师规定未记录时如实标明来源尚未确认。课程 AI 禁止或限制不再作为生成停止条件，用户勾选决定是否使用 AI；
字数、语言和格式等原题要求优先。Codex 不能虚构教师许可，也不能编造个人经历、材料或引用。

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
- 普通 Drive 附件按文件 ID、内容 MD5、大小与格式缓存，元数据版本号变化不会重复下载。重试会校验并复用旧版本缓存中的完整同内容文件；缺失、损坏或内容变化时重新下载。每次同步仍实时核验下载权限。
- Google 原生文档及没有可靠内容校验值的附件按 Drive 版本缓存。提取和转录按内容 SHA-256、处理器版本与配置复用，不因重复下载或重试重复处理。
- 失败保存状态，下一次重试；进程退出后可恢复。单目录文件锁防止重复处理。
- 普通文件下载保留内容校验的 Range 断点，同内容的元数据版本变化可以续传；Google 原生文档中断后重新导出。
- PDF 提取文字并保留全部页面预览；低文字量页面标记为需目视核对，不冒充 OCR 成功。
- DOCX/PPTX 提取段落/幻灯片文字，不运行宏、外链或 XML 实体；保留原文件。TXT/MD/CSV 按行定位。
- Google Docs/Slides/Sheets 导出 PDF。旧 DOC/PPT/XLS、复杂布局和不支持格式保留为需人工处理。
- canDownload 为 false 时不下载，也不把旧附件缓存用于当前检索。快捷方式和不支持的原生文件显示缺口。
- YouTube、其他外部网站仅记录链接；标准 Google Forms 作答页面只读提取题目；用户要求作答时自动预填原表单，绝不提交。

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
Codex 子进程参数与结果核验、未勾选 AI 时不启动模型、前端本机访问与跨站边界、审核包导出。
它们不能证明真实 OAuth、学校 API、模型生成或 Buzz 推理成功。

## 官方参考

- [Classroom Python OAuth](https://developers.google.com/workspace/classroom/quickstart/python)
- [只读权限](https://developers.google.com/workspace/classroom/guides/auth)、[本人提交状态](https://developers.google.com/workspace/classroom/reference/rest/v1/courses.courseWork.studentSubmissions/list)、[UTC 截止时间](https://developers.google.com/workspace/classroom/reference/rest/v1/courses.courseWork)
- [Drive 下载权限](https://developers.google.com/workspace/drive/api/guides/manage-downloads)、[Desktop OAuth / PKCE](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Buzz CLI 与后端](https://chidiwilliams.github.io/buzz/docs/cli)、[Whisper.cpp](https://github.com/ggml-org/whisper.cpp)
- [Codex Skills](https://learn.chatgpt.com/docs/build-skills)

表单核心接口为 `form_fill.prepare_response_forms`、`validate_answers`、`prefill_url` 和 `fill_response_forms`，可独立用于 CLI/后续 MCP。表单答案和包含答案的预填 URL 仅保存在 Git 外私人目录。原生预填支持文字、单选、复选、下拉及量表；未知题型和文件上传如实报告，不伪称已填写。Google 登录限制或原题变化会停止填写并保留真实初稿。参考：[Google 原生预填](https://support.google.com/docs/answer/2839588?hl=en-GB)、[Google 草稿自动保存](https://support.google.com/docs/answer/10952360?hl=en-GB)。

每项审核记录均可“补充并继续”，保存作业专属说明与真实个人事实，再用所选模型处理；“接收 Codex 对话补充”只读取记录会话中已完成的后续回合，不发送消息、不修改原始生成记录。

学校登录受限的 Forms 可在已授权浏览器读取原生题目数据后用 `import-form-questions` 导入。仅缓存解析后的题目，绑定学校账户与原作业链接，24 小时后需重新读取；匿名 403/404 不复用旧导入，禁止提交表单。未完整读题时显示待补充，不能猜测题目或假报自动填写成功。


“补充并继续”会按已读取的原表单显示待补充题目和原选项。逐题回答单独保存在私人目录，点击“保存回答并自动填入”合并到已有模型答案，不重新调用 AI，也不改写原模型结果或出处。必填个人情况缺失时保持空白，其他可用答案照常预填；相同回答重复执行不重复打开表单。运行期间可保存逐题回答，补充说明的修改仍等待本次处理结束。
