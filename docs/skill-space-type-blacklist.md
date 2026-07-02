# 空间类型技能黑名单 (space-type skill blacklist)

## 背景

全局技能通过软链注入每个用户的 `hermes_home/skills/`(见 `hermes_broker.py` `_spawn` +
`proxy_skills_list` 的 sync)。但有些技能**只对特定空间类型有意义**,例如 `mock-interview`
是 talent 空间专属,对 hiring 空间应隐藏。

原有的 `.skipped/{skill_name}` 是**用户级手动**屏蔽 —— 它只对**已存在**的用户目录生效,
**不会传播到未来同空间类型的新用户**。结果:运维给现有 hiring 用户建了 `.skipped/mock-interview`,
但下一个新 spawn 的 hiring 用户 broker 又会自动软链它。

空间类型黑名单补上这个缺口:让"某技能对某空间类型隐藏"成为**持久、自动**的策略。

## 机制

### 配置文件

`/root/.hermes/skills_space_blacklist.json`:

```json
{
  "hiring": ["mock-interview"]
}
```

key = 空间类型(从 waw user_id `waw:<cuid>:workspace:<type>` 的尾部解析),value = 该空间屏蔽的
技能名列表。

### broker 改动(`patches/broker-space-type-skill-blacklist.patch`)

新增三个函数(顶部常量区):

| 函数 | 作用 |
|---|---|
| `_parse_space_type(user_id)` | 从 `waw:...:workspace:talent` 解析出 `talent`;普通/测试用户(无 `:workspace:`)返回 None |
| `_load_space_blacklist()` | 读配置,mtime 变化时自动重载(运维改文件无需重启 broker) |
| `_is_space_blacklisted(user_id, skill_name)` | 该用户的 space_type 是否屏蔽该技能 |

两处注入点都接入:

1. **`_spawn`(新用户首次 spawn)**:`.skipped` **或** 空间黑名单命中 → 不软链
2. **`proxy_skills_list` sync(已存在用户)**:黑名单命中 + 残留软链 → **主动移除** + reload

### 关键:sync 的反向移除

`.skipped` 只判断"不存在才建"。若技能在黑名单生效前已被软链,sync 不会清它。所以黑名单分支里
专门加了反向移除:`blacklisted and user_skill.is_symlink()` → `unlink()`。这让"先软链后加黑名单"
的场景也能收敛(下次该用户列技能时自动清掉)。

## 运维操作

### 屏蔽某技能对某空间

```bash
# 编辑配置 (broker 热加载, 无需重启)
vi /root/.hermes/skills_space_blacklist.json
# 加: {"<space_type>": ["<skill-name>", ...]}

# 让已存在用户立即生效: 触发一次 proxy_skills_list (前端打开技能页就会调),
# 或逐个用户触发。sync 会移除残留软链并 reload。
```

### 解除屏蔽

从配置删掉该技能名。注意:解除后已存在用户需要 sync 才会**补建**软链(`proxy_skills_list`
对"不存在才建"的处理已经覆盖);新用户 spawn 时自动软链。

## 验证(2026-07-02 已测)

- `_is_space_blacklisted`:hiring/mock-interview → True;talent/mock-interview → False;
  hiring/其他技能 → False;expert/mock-interview → False;普通用户 → False(全部正确)
- **全新 hiring 用户**模拟 spawn:软链 23 个技能,mock-interview 被屏蔽 ✅
- **全新 talent 用户**模拟 spawn:软链 24 个技能,含 mock-interview ✅
- **sync 反向移除**:给 hiring 用户临时塞 mock-interview 软链,触发 sync 后被移除 ✅
- 配置 mtime 热加载正常 ✅

## 覆盖范围

- ✅ 现有用户(手动 `.skipped` 已建 + sync 反向移除兜底)
- ✅ 未来新用户(spawn 时空间黑名单判断)
- ✅ 配置热加载(改文件无需重启 broker)

空间类型从 user_id 解析,仅 `waw:...:workspace:<type>` 格式生效;普通/测试用户无空间类型,
不受此机制约束(全部软链)。
