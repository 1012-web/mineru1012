# MinerU 精准解析 API

官方文档：<https://mineru.net/apiManage/docs>

## 适用范围

这里处理用户本地的 PDF。远程公开 URL 可直接用 `/api/v4/extract/task`，但本地文件必须先通过 `/api/v4/file-urls/batch` 取得预签名上传地址。

## 限制与额度定义

- 单文件不超过 200 MB、200 页。
- 本地文件一次最多申请 50 个上传地址；客户端会自动分批。
- 每日可解析文件总数 = 基础额度 + 用户额外申请额度，每日 0 点重置。默认配置 5000 份/日。
- 高优先级解析额度默认 1000 页/日。超出部分自动进入普通队列依次处理，不是拒绝解析。
- 官方 API 文档没有查询当前用量或剩余额度的端点。`mineru_api.py` 只能记录由本机客户端提交的文件/页数，因此报告必须标为估算，不能冒充后台官方余额。

用户后台额度不同可重新配置：

```bash
python scripts/mineru_api.py configure --daily-file-quota 8000 --priority-page-quota 1500
```

## 认证

请求头：

```text
Authorization: Bearer <token>
```

不要把 Token 放进聊天、命令行参数或仓库。配置文件位置：

- Windows：`%APPDATA%\mineru-skill\config.json`，Token 由当前 Windows 用户的 DPAPI 加密。
- Linux/macOS：`${XDG_CONFIG_HOME:-~/.config}/mineru-skill/config.json`，文件权限设为 `0600`。
- `MINERU_API_TOKEN` 环境变量优先于配置文件，适合 CI 或已有密钥管理器的环境。

## 本地文件流程

1. `POST /api/v4/file-urls/batch`：提交文件名、`data_id`、`is_ocr` 与模型配置，取得 `batch_id` 和顺序对应的 `file_urls`。
2. 对每个预签名 URL 执行 `PUT`，只发送文件字节和 `Content-Length`，不要设置额外的 `Content-Type`。
3. `GET /api/v4/extract-results/batch/{batch_id}`：每 5 秒轮询，直到每项为 `done` 或 `failed`。
4. 下载 `full_zip_url`。当前官方 v4 ZIP 的行级版式中间产物通常名为 `*_middle.json`；旧产物可能叫 `layout.json` 并额外带 `block_list.json`。客户端会把实际上传分卷补为 `*_origin.pdf`，后续以 `*_middle.json` / `layout.json` 为正文来源。

超限 PDF 通过 `pypdf` 复制页面对象拆分，不渲染、不重新编码图像，因此不会降低扫描画质。分卷缓存在输出目录的 `_upload_cache/<来源指纹>/`；源路径、文件大小或修改时间变化时会自动使用新缓存。

客户端默认配置：

| 参数 | 默认 | 说明 |
|---|---:|---|
| `model_version` | `vlm` | 官方推荐；可用 `--model pipeline` 覆盖 |
| `language` | `ch` | 可用 `--language` 覆盖 |
| `is_ocr` | 自动 | 抽样文字层，文本过少时开启；可用 `--ocr on/off` 强制 |
| `enable_formula` | `true` | 用 `--no-formula` 关闭 |
| `enable_table` | `true` | 用 `--no-table` 关闭 |

## 命令

```bash
# 首次配置
python scripts/mineru_api.py configure

# 提交、等待、下载并解压
python scripts/mineru_api.py submit book.pdf -o MinerU

# 中断后继续已有任务
python scripts/mineru_api.py resume <batch_id>

# 查看本机当天记录的估算用量
python scripts/mineru_api.py usage
```

`submit` 默认等待 6 小时，轮询间隔 5 秒；可用 `--timeout` 和 `--poll-interval` 调整。任务创建后会在用户配置目录的 `jobs/` 保存恢复信息。

## 完成报告

输出目录内的 `_mineru_api_report_*.json` 是唯一正式报告。交付说明至少包含：

- 原始 PDF 和实际上传分卷；
- 每卷页数、OCR 判定、批次 ID；
- 成功解压目录或失败原因；
- 当日本机追踪的文件/页数；
- 相对于已配置每日文件额度和高优先级页数的估算剩余量；
- 明示本机估算不包含官网、其他设备或其他客户端调用。
