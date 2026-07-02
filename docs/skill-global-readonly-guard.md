# 全局技能只读守卫 (skill_manage write guard)

## 背景

运维反馈安全设计缺陷:"任意一个用户可以创建个全局技能,并且可以修改全局技能"。

## 全链路调查结论

### 架构事实

Hermes Platform 是多租户的:`hermes_broker.py` 为每个用户 spawn 一个独立的 dashboard 进程,
`HERMES_HOME` 指向该用户私有的 `{work_root}/{user_id}/hermes_home/`。全局系统技能本体位于
`/root/.hermes/skills/`,broker 在 `_spawn` 时把它们以**符号链接**形式注入到每个用户的
`hermes_home/skills/`(见 `hermes_broker.py:444-456`):

```python
os.symlink(str(skill_entry), str(dst / skill_entry.name))
```

因此用户侧看到的 `skills/<name>` 是软链,`resolve()` 后指向 `/root/.hermes/skills/<name>` 本体 ——
**全平台 70+ 用户共享同一份物理数据**。

### 写技能的两条入口

1. **agent 工具** `skill_manage`(action: `create`/`edit`/`patch`/`write_file`/`remove_file`/`delete`)
   —— 用户在对话里引导 agent 调用。
2. **dashboard HTTP 端点**(`PUT /api/skills/content`、`POST /api/skills` 等)
   —— 实现上也调用同一批 `skill_manager_tool.py` 内部函数。

两条入口的写动作最终都走 `skill_manager_tool.py` 里的 `_edit_skill` / `_patch_skill` /
`_write_file` / `_remove_file` / `_delete_skill`。这些函数靠 `_find_skill(name)` 定位技能后直接
`_atomic_write_text` 覆写,**原本没有任何"全局只读 / 系统技能"概念**
(grep `readonly|is_global|system_skill|managed|immutable` 全部零命中)。

### 关键发现:`_find_skill` 当前不跟随目录软链

`_find_skill`(第 ~360 行)用 `Path.rglob("SKILL.md")`,而 Python 的 rglob 底层是
`os.walk(followlinks=False)` —— **不进入目录符号链接**。实测(broker venv Python 3.11.15):

| 技能在用户目录下的形态 | `_find_skill` 能否找到 |
|---|---|
| 软链(→ 全局本体),如 `campus-resume-targeted-diagnosis` | **找不到** |
| 真实目录,如 `resume-intake` / `mock-interview` | 能找到 |

这**与项目另一处遍历不一致**:`agent/skill_utils.py:706` 用 `os.walk(skills_dir, followlinks=True)`
做技能扫描/列表,会跟随软链 —— 所以 dashboard 能**列出**软链技能,但 `skill_manage(edit)` 却
**找不到**它们。这种不一致更像是巧合而非刻意的安全设计。

### 修正最初判断

最初(基于读代码)判断"任意用户能通过 skill_manage 改全局技能"。实测后修正:

- **当前(rglob 不跟随软链)→ 通过 skill_manage / HTTP 端点改软链全局技能并不可达**,
  因为 `_find_skill` 对软链技能返回 None,直接 "not found"。
- **但风险是真实的、且不依赖当前巧合**:
  1. rglob 是否跟随软链是 Python 版本相关行为,升级后可能改变;
  2. 项目内已有 `followlinks=True` 先例,任何把 `_find_skill` 改成跟随软链的改动都会复活漏洞;
  3. external_dirs / profile 切换等其它查找路径可能绕过当前 rglib 行为;
  4. delete 路径已有专门的软链防护(`_validate_delete_target`),说明项目作者**意识到**了软链
     技能被误删/误改的风险 —— edit/patch/write_file/remove_file 缺失对称防护是遗漏。

## 修复:统一只读守卫

`patches/skill-manage-global-readonly-guard.patch` 在 `skill_manager_tool.py` 新增
`_managed_skill_write_guard(skill_dir)`,并在 `_edit_skill` / `_patch_skill` / `_write_file` /
`_remove_file` / `_delete_skill` 入口统一调用(与 delete 路径的 `_validate_delete_target` 并列,
形成纵深防御)。

### 判据

```python
resolved = skill_dir.resolve()           # 跟随软链到真实路径
private_root = SKILLS_DIR.resolve()      # 本进程私有技能根 (= HERMES_HOME/skills)
if resolved 不在 private_root 之下:
    拒绝 ("shared/global skill ... read-only via symlink")
```

即:**只要技能的真实位置不在本用户私有技能根内,就视为只读全局技能,拒绝写/删**。
这覆盖了所有"通过软链 / external_dir / profile 指向外部只读根"的情况,且不依赖 rglob 的跟随行为。

### 运维 escape hatch

平台管理员需要升级全局技能本体时,在相应进程设置:

```bash
export HERMES_SKILLS_WRITABLE_GLOBAL=1
```

守卫即放行(`is_truthy_value` 判定)。正常 broker spawn 的用户进程不设此变量,始终受保护。

### 验证

`/tmp` 下最小复现(模拟 broker 软链注入):
- 软链技能 → edit/patch/write_file/remove_file **全部拦截** ✅
- 用户私有真实技能 → edit/write_file **放行**(功能不回归)✅
- `HERMES_SKILLS_WRITABLE_GLOBAL=1` → 放行 ✅

### `create` 动作为何不守卫

`_create_skill` 写入 `SKILLS_DIR = HERMES_HOME/skills` = 用户私有根,天然不碰全局本体,
故无需守卫。用户可以自由创建私有技能 —— 这是受允许的功能。

## 第二道闸:file / patch / execute_code 工具(`agent/file_safety.py`)

skill_manage 守卫只覆盖**那一个工具**的写动作。但 agent 还有 `file`/`file_operations`/`patch`
/`execute_code` 等工具能直接 `open(path, 'w')`,它们走的是另一条统一闸口
`agent/file_safety.py::is_write_denied(path)`,**完全不过 skill_manage 守卫**。实测:软链技能路径
`<user_home>/skills/<name>/SKILL.md` 会被 `is_write_denied` 放行(realpath 解析到全局本体,但原
黑名单不含普通技能),内核跟随软链写入 → 同样能改全局。

因此同一判据必须在 `is_write_denied` 上再设一道(纵深防御,与 skill_manage 守卫互为兜底)。

### file_safety 侧的实现

`is_write_denied` 现已带软链逃逸检测(内联,在原黑名单/safe_root 检查之间):

```python
# 呈现路径(raw, 不跟随软链)是否在 skills 命名空间内?
presented_in_skills_ns = abspath(raw) 在 private_skills_root 内
# 真实路径(跟随软链)是否逃出私有根?
resolved_inside = resolved 在 private_skills_root 内
if presented_in_skills_ns and not resolved_inside:
    return True   # 软链逃逸 → 全局技能 → 拒绝
```

关键点:
- **同时需要 `raw`(原始路径)和 `resolved`(realpath)**。只看 resolved 无法区分"软链技能"
  和"用户项目里碰巧叫 skills 的目录"(`abspath(raw)` 在 ns 内、`resolved` 也在内 → 真实私有,放行)。
- **`_hermes_root_path()` 在 broker 下不可用作全局锚点**:broker 把 `HERMES_HOME` 设成 user_home,
  `get_default_hermes_root()` 会返回 user_home(非 `/root/.hermes`)。所以判据用"私有根内/外"
  而非"是否在全局根内"。
- escape hatch `HERMES_SKILLS_WRITABLE_GLOBAL=1` 同时让本守卫和 safe_root 检查放行(hatch 全开)。

### 验证(broker venv,真实用户目录)

- 软链技能 SKILL.md / 子文件 / 直接全局路径 → **全部拒绝** ✅
- 工作区普通文件、uploads、用户项目里叫 skills 的目录 → **放行**(不回归)✅
- 原有黑名单(`.hub`、`.ssh`)→ **仍拒绝**(不回归)✅
- `HERMES_SKILLS_WRITABLE_GLOBAL=1` → **放行** ✅

## 生效方式

补丁就地修改两个文件:
- `/root/.hermes/hermes-agent/tools/skill_manager_tool.py`(skill_manage 闸)
- `/root/.hermes/hermes-agent/agent/file_safety.py`(file/patch/execute_code 闸)

broker spawn 的每个 dashboard 进程 import 这两个模块,故 **重启 broker 后**所有新会话即受双重保护。
重启流程见 memory `reference_broker_restart.md`(需 `env -i` 防 Claude Code 环境污染)。

## 残留边界(诚实记录,实测坐实)

两道应用层闸覆盖了 agent 的**结构化写工具**(skill_manage / file / patch / dashboard HTTP)。
但 broker 环境下还有**应用层守卫无法覆盖的路径**:

**实测结论(broker 进程 root + `env_type=local`,沙箱=同权限本地子进程,无 chroot/seccomp):**

| 攻击路径 | 状态 | 原因 |
|---|---|---|
| `skill_manage` / `file` / `patch` / dashboard HTTP | ✅ 封死 | 应用层闸 |
| `execute_code` 沙箱 `open('/root/.hermes/skills/...','w')` | ❌ **未封** | 子进程系统调用,不过 `is_write_denied` |
| `terminal`(bash)`echo > /root/.hermes/skills/...` | ❌ **未封** | local 后端,命令级 guard 不拦普通写 |

实测脚本(沙箱直接 append 写全局技能 SKILL.md → 成功,而 `is_write_denied` 对同路径返回 True
却拦不到,因为沙箱 `open()` 不经过它)。**根因**:broker + 所有 dashboard 进程跑在 root(uid=0),
沙箱是同权限本地子进程,任何能执行系统调用的子进程都能直接写 `/root/.hermes`。

**这决定了应用层守卫的性质**:它们挡住"用户用正常/便利方式改技能"的路径(占绝大多数实际场景),
但**不是真正的多租户安全边界**。要做成硬隔离(可承诺给租户),只有两条真路:

1. **bind-mount ro + per-user mount namespace**(治本):broker spawn 改成每用户 `unshare -m`
   独立挂载命名空间,全局技能只读挂入。租户进程内核级写不了,运维进程不受影响。需 CAP_SYS_ADMIN,
   架构改动较大。
2. **文件系统不可变(chattr +i /root/.hermes/skills)**(半治本,改动小):root 也写不了,内核强制。
   运维升级需 `chattr -i` 解锁→改→重新 `+i`。适合"技能不常变"的现状。

**当前决策(2026-07-02):已知风险记录,暂不处理。** 理由:结构化写工具已封死,覆盖绝大多数实际
场景;execute_code/terminal 直写需用户主动构造路径且具备引导 agent 跑代码的能力,威胁等级取决于
用户可信度(内部团队 vs 公开租户)。待威胁模型升级时再做内核级隔离。

## 第三道闸:文件系统不可变(`chattr +i`,内核级硬边界)

上面两道应用层闸拦不住 execute_code/terminal 的子进程 syscall(见"残留边界")。最终通过
`chattr +i` 给全局技能本体加**内核级不可变属性**补上:root 也写不了,内核强制(EPERM),
对所有进程(含已运行的 dashboard、沙箱子进程)即时生效,无需重启 broker。

### 实现(`scripts/skill_tree_immutable.sh`)

精确锁定**技能本体目录**(含 SKILL.md 的目录,共 54 个,含 productivity/research/software-development
等类别下的子技能),**递归** `chattr -R +i`。跳过运行时要写的元数据/缓存,确保功能不 break:

| 跳过项 | 原因 |
|---|---|
| `.hub/`(index-cache/lock.json/audit.log) | skill hub 索引,运行时写 |
| `.archive/` `.curator_backups/` | curator 归档 |
| `.curator_state` `.bundled_manifest` `.usage.json` | curator/usage 运行时状态 |
| `__pycache__` | Python 运行时生成 |
| `credential-bootstrap.py` | 运维脚本 |

子命令:
```bash
scripts/skill_tree_immutable.sh lock     # 锁定全局技能本体
scripts/skill_tree_immutable.sh unlock   # 解锁(运维升级前调用)
scripts/skill_tree_immutable.sh status   # 查看锁定状态
scripts/skill_tree_immutable.sh list     # dry-run 列出将锁的目录
```

### 运维升级全局技能的流程

```bash
scripts/skill_tree_immutable.sh unlock                    # 1. 解锁
# 2. 编辑 /root/.hermes/skills/... (复制源技能、改 SKILL.md 等)
scripts/skill_tree_immutable.sh lock                      # 3. 重新锁定
# 4. 新建/重连的 session 会加载新内容;已运行 session 需重连或 reload.mcp
```

### 验证(ext4,已上线)

- 锁定 54 个技能本体目录,读写分离:读取/遍历(`os.walk followlinks=True`)正常,**写入/删除/建文件全部 EPERM** ✅
- execute_code `open()`、terminal `echo >>` 直写锁定技能 → **内核拒绝** ✅(应用层闸拦不到的子进程 syscall,被内核拦下)
- 用户创建私有技能(`skill_manage create` 写私有根)→ **正常** ✅(不受全局锁影响)
- `.hub`/curator/usage 等运行时目录 → **仍可写** ✅(功能不回归)
- 对**已运行**的 dashboard 进程即时生效,无需重启 broker

### 边界(诚实记录)

`+i` 是当前部署下最硬的边界(ext4 支持)。理论上需 CAP_LINUX_IMMUTABLE 的进程仍可 `chattr -i`
解锁——但 broker/沙箱进程没有这个 capability(`chattr` 需 root + 该 cap,普通沙箱代码调
`os.chflags` 会被拒)。所以对租户进程 = 不可写。唯一能解锁的是有 root+cap 的运维操作。



