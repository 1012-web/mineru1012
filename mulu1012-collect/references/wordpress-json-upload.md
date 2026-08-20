# WordPress 上传：通道与机制

**通道已定（2026-08-02）：`mulu1012-suite` 的 catalog-import REST。**

```
POST /mulu1012/v1/catalog-import/preflight   验证，不写入（即 dry-run）
        ↓ 用户确认
POST /mulu1012/v1/catalog-import/apply       写入
```

**不用**这两条旧通道（仍在线但已弃用）：

- `mulu-database-importer` 的 ability `mulu1012-catalog/import-sites-batch`——缺词直接建、无同名歧义检查
- `POST /wp/v2/sites` + `mulu_taxonomy_terms` 参数——只收词名不支持层级路径，是根级重复词的成因

## 契约去哪查（本文不复述）

| 要查什么 | 去哪 |
|---|---|
| 入参结构、受控清单、错误码 | 代码仓 `D:\projects\mulu1012\00-开发文档\插件说明（plugins）\sites题录批量导入（catalog-import）.md` |
| 字段键名、存放层、类型 | `sites字段一览.md` §二（字段总表）、§三（子字段总表） |
| 字段怎么填、受控词表 | `字段填写指南.md`；词表实况看 `词项树快照.md` |
| 层级路径语法、祖先链规则 | [`编目-分类法层级规则.md`](编目-分类法层级规则.md) |

键名会随字段改造变化（如 `favorites` → `site_catalog`），任何本地映射表都会静默过期——**上传前逐字段对照上面的文档，不凭本文或记忆拼 JSON**。

## 上传前检查清单

- [ ] 键名与存放层逐字段对照过 `sites字段一览.md` §二
- [ ] taxonomy 词名与站上完全一致（查 `词项树快照.md`）；层级分类法写满祖先链、拆成多条路径；扁平分类法不用 `>`
- [ ] 入口 url 以 `http(s)://` 开头，标题非空，受控字段的值都在词表内
- [ ] 入库查重分流已处理：判「已存在」的不新建条目，该馆授权汇进已有 WPID 的机构授权明细
- [ ] **先小批 preflight，逐条核对回执**——尤其 `created_terms`，不该新建的词出现在里面就停下
- [ ] suite 本地 checkout 的受控白名单与线上分类法有出入，以线上与 preflight 回执为准

## 上传后

逐条取 `id` 回填批次总目的 `WPID` 列。**WPID 只能来自线上响应或 REST 回读，绝不凭记忆填**；回填后再回读复核一次，确认标题、入口 URL、关键 taxonomy 都写进去了。

## 认证

WordPress Application Password（Basic Auth），密钥文件 `C:\Users\yan\.codex-secrets\mulu1012-wordpress.json`。**只读取，内容绝不进代码、报告、对话**；报告里只写「已认证 / 上传成功 / 上传失败」。

不直接写 WordPress MySQL。
