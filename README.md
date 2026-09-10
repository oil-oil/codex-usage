# codex-usage

从本机 Codex SQLite 数据生成中文历史 Token 报告。

完整执行规则见 [SKILL.md](SKILL.md)。

## 配置、依赖与使用边界

Python 3 与可读取的 Codex 状态数据库。无需额外账号或 API Key；可用 --db 明确指定数据源。

只读本地历史统计，不代表账户剩余额度或账单；导出可能包含任务标题与路径，分享前检查范围。

使用示例：

```text
生成本机 Codex 的月度 Token 报告，输出到我指定的目录。
```

## GitHub 安装

把 [仓库地址](https://github.com/oil-oil/codex-usage) 交给 Agent，要求按 README 安装；也可运行：

```bash
npx skills add oil-oil/codex-usage
```

安装后由宿主重新加载 Skill。
