# HiThink 同花顺 MCP + Cursor 接入指南

> 把 HiThink 同花顺金融数据服务接入 Cursor，让 AI Agent（你自己）能直接对话查行情、查涨跌停、查龙虎榜、查财务。
> 配置完成后，您可以在 Cursor 对话中直接问："贵州茅台今天涨了多少？" 或 "今天涨停板有哪些？"

## 一、MCP 服务端点

根据 HiThink 官方文档，6 个 MCP 服务端点：

| 服务名 | 端点 | 适用场景 |
|--------|------|----------|
| `fuyao-a-share-mcp` | `https://fuyao.aicubes.cn/mcp/a-share` | A 股行情 / 历史 K / 涨停池 / 龙虎榜 |
| `fuyao-a-share-index-mcp` | `https://fuyao.aicubes.cn/mcp/a-share-index` | 指数 / 板块行情 |
| `fuyao-meta-mcp` | `https://fuyao.aicubes.cn/mcp/meta` | 标的检索 / 标的列表 |
| `fuyao-fund-mcp` | `https://fuyao.aicubes.cn/mcp/fund` | ETF / 基金行情 |
| `fuyao-futures-mcp` | `https://fuyao.aicubes.cn/mcp/futures` | 期货（V26.88 用不到） |
| `fuyao-options-mcp` | `https://fuyao.aicubes.cn/mcp/options` | 期权（V26.88 用不到） |

## 二、Cursor MCP 配置

在 `~/.cursor/mcp.json`（macOS/Linux）或 `%USERPROFILE%\.cursor\mcp.json`（Windows）添加：

```json
{
  "mcpServers": {
    "hithink-a-share": {
      "command": "npx",
      "args": ["-y", "hithink-finance-mcp", "--domain", "a-share"],
      "env": {
        "HITHINK_API_KEY": "${HITHINK_FINANCE_API_KEY}"
      }
    },
    "hithink-a-share-index": {
      "command": "npx",
      "args": ["-y", "hithink-finance-mcp", "--domain", "a-share-index"],
      "env": {
        "HITHINK_API_KEY": "${HITHINK_FINANCE_API_KEY}"
      }
    },
    "hithink-meta": {
      "command": "npx",
      "args": ["-y", "hithink-finance-mcp", "--domain", "meta"],
      "env": {
        "HITHINK_API_KEY": "${HITHINK_FINANCE_API_KEY}"
      }
    },
    "hithink-fund": {
      "command": "npx",
      "args": ["-y", "hithink-finance-mcp", "--domain", "fund"],
      "env": {
        "HITHINK_API_KEY": "${HITHINK_FINANCE_API_KEY}"
      }
    }
  }
}
```

> 如果 Cursor 暂不支持 npx 启动，可改用 HTTP 远程 MCP（按官方最新语法调整）。

## 三、设置 API Key

环境变量优先级最高（避免写入仓库）：

```bash
# Windows PowerShell
[System.Environment]::SetEnvironmentVariable('HITHINK_FINANCE_API_KEY', 'your-api-key-here', 'User')

# 或在 config.yaml:hithink.api_key 填写（次优先）
```

## 四、Cursor 中直接调用

配置完成后，IDE 重启后即可在对话中提问：

| 用户提问 | 系统调用 |
|----------|----------|
| "贵州茅台今天收盘多少？" | `prices_snapshot(thscode='600519.SH')` |
| "今天涨停的股票有哪些？" | `special_data_limit_up_pool(date_ms=<今天>)` |
| "最近 5 个交易日贵州茅台的 K 线" | `prices_historical(thscode='600519.SH', start_date=..., end_date=...)` |
| "今天龙虎榜的机构买入 TOP10" | `special_data_dragon_tiger_list(rank_type='institution')` |
| "沪深 300 今天的涨跌" | `index_prices_snapshot(thscode='000300.SH')` |
| "苹果公司股票代码" | `meta_tickers_search(query='apple')` |

## 五、配套小杰系统

| 文件 | 作用 |
|------|------|
| `data/hithink_client.py` | HiThink HTTP REST 客户端（V26.88+ 已落地） |
| `data/hithink_diff_monitor.py` | 双源校验（与 Tushare 比对） |
| `data/hithink_special_sync.py` | 特色数据落库（涨停 / 龙虎榜 / 热股榜） |

## 六、官方参考

- HiThink 文档站：https://fuyao.aicubes.cn/
- HiThink llms.txt：https://fuyao.aicubes.cn/llms.txt
- HiThink llms-full.txt：https://fuyao.aicubes.cn/llms-full.txt
- 注册 API Key：https://fuyao.aicubes.cn/admin
