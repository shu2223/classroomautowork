# 核心模块与持久化

```mermaid
flowchart LR
  UI[本地前端：刷新 / 勾选 / 一键审核] --> J[持久 FrontendJobs]
  J --> W[Python workflow]
  S[Codex Skill / CLI] --> W
  W --> G[固定只读 GoogleReader]
  G --> C[Classroom / Drive]
  W --> T[SQLite 任务与来源索引]
  W --> E[PDF / OOXML 提取]
  W --> B[Buzz 本地 Whisper.cpp]
  T --> R[本地 BM25 检索]
  R --> D[Codex 按课程规则撰写]
  D --> V[本地审核校验]
  V --> H[用户审阅与手动提交]
```

## 公共调用

```python
from classroomautowork.config import Settings
from classroomautowork.workflow import prepare

settings = Settings.load()  # Git 外的真实用户配置；OAuth 必须先完成
result = prepare(settings, due_before="2026-10-10", progress=print)
# 明确勾选的作业，不扩展到其他任务；重新读取本人提交状态后处理：
# result = prepare(settings, targets=[("真实数字课程ID", "真实数字作业ID")])
for package in result["packages"]:
    print(package["package"], package["can_draft"])
```

prepare 不依赖 argparse，不生成模型答案；连接、附件提取、检索与审核均可独立调用。
GoogleReader 只公开明确读取方法，没有通用 HTTP 路由或由资料决定的方法名。
测试可替换 reader，但生产 CLI 没有 fake/mock 成功模式。

## 前端适配与任务

webapp 只监听随机端口的 127.0.0.1。HTML/CSS/JS 都来自本机包，无 CDN。
API 需要随机本机访问凭证，校验 Host，写操作同时校验 Origin；不接受命令、解释器、权限或任意文件路径。
所有课程文字以 textContent 呈现，来源链接只作为用户手动打开的链接。

FrontendJobs 把真实待办刷新时间和每批明确选择写入 Git 外的 ui/，一个助手实例只运行一个任务。
资料准备复用 Store 的跨进程锁及内容缓存；safe-boundary 暂停不会被吞成附件错误。
重启将未完成的前端记录标成 interrupted，用户重试时重新核对远端状态并复用已完成阶段。

未知或禁止 AI 规则时保存原要求为 needs_user 检查并本地 finalize，不启动模型，不写假初稿。
允许时 drafting.generate_review 调用本机 Codex CLI，提示通过 stdin 传递，不经过命令 shell。
不改变模型、沙箱、审批、hook 或规则；程序再次 finalize 验证真实输出，失败不标成完成。
审核复用要求文件校验值、当前来源与课程规则都仍匹配。预览/导出不暴露未核验初稿，规则变化后要求重新准备。
ZIP 导出仅包含已知审核文件，拒绝跨目录链接；页面图像只读取已记录的 processed/ PNG。

## 缓存与恢复

Store 在 Git 外使用 SQLite WAL 和跨进程文件锁。键为阶段名与输入指纹的 SHA-256。
状态为 running / done / failed / interrupted，记录尝试次数、更新时间及安全错误。
新进程持锁后将遗留 running 改成 interrupted；失败/中断阶段在下次调用重做。
done 的声明输出必须存在且 SHA-256 一致才能复用。

阶段为 classroom-snapshot、drive-download、document、media、transcript-segment、review-preparation。
缓存命中不代表远端权限仍有效；每次 sync 先读最新 metadata，并在下载后核对版本与权限。
来源索引只保留本次成功读取/处理的源，不把撤权或读取失败的陈旧文字当成现有证据。
原缓存为恢复保留在私人目录，不参与当前检索。

同一 source 更新时替换 chunks；每段带源类型、标题、链接、locator、revision 和稳定 ID。
中文、日文按双字切分，拉丁词分词，BM25 排序。按课程隔离，不索引其他学生答案。
Buzz 先由本地 ffmpeg 转成 16kHz 单声道 PCM，每五分钟独立缓存，字幕加上片段时间偏移。
模型与引擎身份变化失效推理缓存；课程内容从不传给 shell。

## 审核与信任

用户维护私人配置。课程内容只提供资料和作业约束，不能设置权限或审批。
manifest/evidence/requirements 是准备阶段的输入，不允许为通过校验而修改。
finalize 检查来源是否仍匹配当前索引，核对课程配置、引文、个人事实索引和显示的证据 ID，
生成 checksum receipt 后停止。机械检查不判断所有主张是否被证据蕴含，不代替人工审阅。
真实验证与离线测试分离：token 在 OS 凭据库，connection-receipt 在私人目录。
源码不携带账户、客户端 JSON、课程 ID、材料、缓存或真实验证样本。
