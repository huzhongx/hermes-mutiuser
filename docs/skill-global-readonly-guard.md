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

## 生效方式

补丁就地修改 `/root/.hermes/hermes-agent/tools/skill_manager_tool.py`。broker spawn 的每个
dashboard 进程 import 该模块,故 **重启 broker 后**所有新会话的 agent 工具调用即受保护。
重启流程见 memory `reference_broker_restart.md`(需 `env -i` 防 Claude Code 环境污染)。
