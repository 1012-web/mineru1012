# 站点探测方法论

## 概述

探测是采集的第一步。目标是搞清楚目标导航站的页面结构、可用字段、分页机制和反爬措施。**不凭记忆或经验判断，必须实际抓取样本页面验证。**

## 探测步骤

### 步骤 0：判定站点类型

在开始详细探测前，先判定站点类型，不同站点类型采用不同的探测方法。

| 类型 | 特征 | 典型站点 |
|------|------|---------|
| **学术导航站** | 有详情页，字段丰富（20+），有 CSV/API 导出 | DBIS、CrossAsia、Clio-online |
| **图书馆列表站** | 列表页即全部数据，无详情页，字段嵌入 JavaScript | read.nlc.cn、各图书馆门户 |
| **其他** | 混合型或特殊结构 | 待定 |

```powershell
# 1. 抓取列表页首页
$html = (Invoke-WebRequest -Uri $listUrl -UseBasicParsing -TimeoutSec 30).Content

# 2. 判断是否有详情页链接
$hasDetailLinks = [regex]::IsMatch($html, 'href="[^"]*?resource[^"]*?\d+"', [RegexOptions]::IgnoreCase)

# 3. 判断是否有 JavaScript 数据调用
$hasJsCalls = [regex]::IsMatch($html, "(openOutRes|collect|showDetail)\s*\(", [RegexOptions]::IgnoreCase)

# 4. 判断是否有 CSV/API 导出
$hasExport = [regex]::IsMatch($html, '(export|csv|download|api)', [RegexOptions]::IgnoreCase)
```

### 步骤 1：抓取列表页

```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'

$listUrl = '<目标站分类页面 URL>'
$listHtml = (Invoke-WebRequest -Uri $listUrl -UseBasicParsing -SkipCertificateCheck -TimeoutSec 30).Content
```

### 步骤 2：识别总记录数

```powershell
# 方法 A：分页控件中的总页数
$pageLinks = [regex]::Matches($listHtml, 'value="(\d+)"')
$lastPage = ($pageLinks | ForEach-Object { [int]$_.Groups[1].Value } | Measure-Object -Maximum).Maximum

# 方法 B：分页大小选项中的 "Alle"（全部）
$allOption = [regex]::Match($listHtml, 'value="(\d+)"[^>]*>Alle')
$totalIfAll = $allOption.Groups[1].Value

# 方法 C：CSV 导出中的行数（如有）
$csvRecords = (Import-Csv 'export.csv').Count
```

### 步骤 3：确定字段上限（核心新增）

**这是 NLC 采集经验中最重要的改进。** 在进入详细字段枚举前，先确定"最多能采集到多少字段"，避免投入与回报不匹配。

```powershell
# 取一条记录样本
$sampleCard = [regex]::Match($listHtml, '<li>[\s\S]*?</li>').Value

# 枚举该样本中所有可提取的信息点
$fields = @{}

# 3a. 文本内容
$textMatches = [regex]::Matches($sampleCard, '>([^<]{2,100})<')
foreach ($m in $textMatches) {
    $text = $m.Groups[1].Value.Trim()
    if ($text.Length -gt 2) { $fields["文本: $($text.Substring(0, [Math]::Min(30, $text.Length)))"] = $true }
}

# 3b. HTML 属性中的信息（title, alt, href, src）
$attrMatches = [regex]::Matches($sampleCard, '(title|alt|href|src)="([^"]*)"')
foreach ($m in $attrMatches) {
    $val = $m.Groups[2].Value.Trim()
    if ($val.Length -gt 2) { $fields["$($m.Groups[1].Value): $($val.Substring(0, [Math]::Min(50, $val.Length)))"] = $true }
}

# 3c. JavaScript 函数调用中的参数
$jsMatches = [regex]::Matches($sampleCard, "(\w+)\s*\(([^)]*)\)")
foreach ($m in $jsMatches) {
    $fields["JS: $($m.Groups[1].Value)($($m.Groups[2].Value))"] = $true
}

# 3d. 图片图标（访问方式指示）
$imgMatches = [regex]::Matches($sampleCard, '<img[^>]*title="([^"]*)"')
foreach ($m in $imgMatches) {
    $fields["图标: $($m.Groups[1].Value)"] = $true
}

Write-Host "`n=== 字段上限探测结果 ==="
Write-Host "该站点一条记录中可提取的信息点: $($fields.Count) 个"
$fields.Keys | Sort-Object | ForEach-Object { Write-Host "  - $_" }
```

### 步骤 4：识别列表页卡片结构（学术导航站）

对于学术导航站，列表页每条记录通常是一个 HTML 区块：

```powershell
# 找到第一个卡片区块
$cardPattern = '<div class="box box-(\d+)">'  # DBIS 模式
$firstCard = [regex]::Match($listHtml, $cardPattern)
$firstId = $firstCard.Groups[1].Value

# 提取该卡片内的所有可见字段
$cardHtml = [regex]::Match($listHtml, "<div class=`"box box-$firstId`"">(.*?)</div>\s*</div>\s*</div>", [Singleline])
```

### 步骤 5：抓取样本详情页（学术导航站）

```powershell
# 从列表页提取第一条记录的详情页链接
$detailLinkPattern = 'href=[''"]([^''''"]*resources/\d+)[''"]'  # DBIS 模式
$detailUrl = [regex]::Match($listHtml, $detailLinkPattern).Groups[1].Value

# 如果是相对路径，拼接域名
if ($detailUrl -match '^/') {
    $baseUri = ([System.Uri]$listUrl).GetLeftPart([System.UriPartial]::Authority)
    $detailUrl = $baseUri + $detailUrl
}

$detailHtml = (Invoke-WebRequest -Uri $detailUrl -UseBasicParsing -SkipCertificateCheck -TimeoutSec 30).Content
```

### 步骤 6：枚举详情页字段（学术导航站）

```powershell
# 6a. H2 标题（section 级字段）
Write-Host "=== H2 Sections ==="
[regex]::Matches($detailHtml, '<h2[^>]*>\s*(.*?)\s*</h2>', [Singleline]) | 
    ForEach-Object { $_.Groups[1].Value.Trim() }

# 6b. 表格表头（TH）
Write-Host "`n=== Table Headers ==="
[regex]::Matches($detailHtml, '<th[^>]*>\s*(.*?)\s*</th>', [Singleline]) | 
    ForEach-Object { $_.Groups[1].Value.Trim() }

# 6c. 定义列表（DT）
Write-Host "`n=== Definition Terms ==="
[regex]::Matches($detailHtml, '<dt[^>]*>\s*(.*?)\s*</dt>', [Singleline]) | 
    ForEach-Object { $_.Groups[1].Value.Trim() }

# 6d. 标签-值表格行（TD 成对）
Write-Host "`n=== Table Rows (label-value pairs) ==="
$rows = [regex]::Matches($detailHtml, '<tr[^>]*>(.*?)</tr>', [Singleline])
foreach ($row in $rows) {
    $cells = [regex]::Matches($row.Groups[1].Value, '<td[^>]*>(.*?)</td>', [Singleline])
    if ($cells.Count -ge 2) {
        $label = [regex]::Replace($cells[0].Groups[1].Value, '<[^>]+>', '').Trim()
        $value = [regex]::Replace($cells[1].Groups[1].Value, '<[^>]+>', '').Trim()
        if ($label) { Write-Host "  $label => $($value.Substring(0, [Math]::Min(80, $value.Length)))" }
    }
}

# 6e. 带 class 的标签（如 tag、badge）
Write-Host "`n=== Tag/Badge classes ==="
[regex]::Matches($detailHtml, '<span class="(tag|badge|label)[^"]*"[^>]*>\s*(.*?)\s*</span>', [Singleline]) |
    ForEach-Object { $_.Groups[2].Value.Trim() } | Select-Object -Unique

# 6f. 图片类信息（红绿灯、图标等）
Write-Host "`n=== Image signals ==="
[regex]::Matches($detailHtml, '<img[^>]*class="([^"]*)"[^>]*>', [Singleline]) |
    ForEach-Object { $_.Groups[1].Value.Trim() } | Select-Object -Unique

# 6g. 外部链接（Zugangslink 等）
Write-Host "`n=== External links ==="
[regex]::Matches($detailHtml, '<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', [Singleline]) |
    ForEach-Object { $href=$_.Groups[1].Value; $text=[regex]::Replace($_.Groups[2].Value,'<[^>]+>','').Trim(); if($text){Write-Host "  $text => $href"} }
```

### 步骤 7：枚举列表页字段（图书馆列表站）

对于图书馆列表站，数据全部在列表页，字段通常嵌入在 HTML 和 JavaScript 函数调用中：

```powershell
# 取一条样本记录
$sampleCard = [regex]::Match($html, '<li>[\s\S]*?</li>').Value

# 7a. 提取名称：<span style="overflow: hidden;">名称<img
$nameMatch = [regex]::Match($sampleCard, '<span[^>]*style="overflow: hidden;">(.*?)<img')
if ($nameMatch.Success) { Write-Host "名称: $($nameMatch.Groups[1].Value.Trim())" }

# 7b. 提取资源ID：collect('数字')
$idMatch = [regex]::Match($sampleCard, "collect\('(\d+)'\)")
if ($idMatch.Success) { Write-Host "资源ID: $($idMatch.Groups[1].Value)" }

# 7c. 提取访问方式参数：openOutRes('type','intranet','3','extranet','roles')
$outResMatch = [regex]::Match($sampleCard, "openOutRes\('(.*?)','(.*?)','(.*?)','(.*?)','(.*?)'\)")
if ($outResMatch.Success) {
    Write-Host "资源类型: $($outResMatch.Groups[1].Value)"
    Write-Host "内网URL: $($outResMatch.Groups[2].Value)"
    Write-Host "外网URL: $($outResMatch.Groups[4].Value)"
    Write-Host "授权角色: $($outResMatch.Groups[5].Value)"
}

# 7d. 提取描述：title="..."
$descMatch = [regex]::Match($sampleCard, '<div class="txt"[^>]*title="(.*?)"')
if ($descMatch.Success) { Write-Host "描述: $($descMatch.Groups[1].Value.Substring(0, [Math]::Min(100, $descMatch.Groups[1].Value.Length)))..." }

# 7e. 提取访问方式图标：<img alt="" title="局域网访问资源">
$iconMatches = [regex]::Matches($sampleCard, '<img alt="" title="(局域网访问资源|互联网访问资源)"')
foreach ($icon in $iconMatches) {
    Write-Host "访问方式: $($icon.Groups[1].Value)"
}

# 7f. 提取缩略图：src="...outResImages..."
$imgMatch = [regex]::Match($sampleCard, '<img[^>]*src="([^"]*outResImages[^"]*)"')
if ($imgMatch.Success) { Write-Host "缩略图: $($imgMatch.Groups[1].Value)" }
```

### 步骤 8：检测列表页 vs 详情页字段差异（仅学术导航站）

```powershell
# 统计列表页每条卡片的字段
$cardFields = @('title', 'description', 'traffic-light', 'tags')

# 对比：列表页描述是否被截断
$truncCount = ([regex]::Matches($listHtml, '<span class="wrap-result-text">.*?\.\.\.\s*</span>', [Singleline])).Count
$totalCards = ([regex]::Matches($listHtml, '<div class="box box-\d+">')).Count
$truncRate = if ($totalCards -gt 0) { [math]::Round($truncCount / $totalCards * 100, 1) } else { 0 }
Write-Host "列表页描述截断率: ${truncRate}% ($truncCount/$totalCards)"
```

### 步骤 9：检测反爬措施

```powershell
# 检查 Cloudflare
if ($listHtml -match 'cloudflare|cf-|__cf_bm') {
    Write-Host "检测到 Cloudflare 防护"
}

# 检查登录墙
if ($listHtml -match 'login|anmelden|sign in|password') {
    Write-Host "可能需要登录"
}

# 检查 robots.txt
try {
    $robotsUrl = ([System.Uri]$listUrl).GetLeftPart([System.UriPartial]::Authority) + '/robots.txt'
    $robots = (Invoke-WebRequest -Uri $robotsUrl -UseBasicParsing -SkipCertificateCheck -TimeoutSec 10).Content
    Write-Host "robots.txt 内容:"
    Write-Host $robots
} catch { Write-Host "无 robots.txt" }

# 测试连续请求频率（发 10 个请求看是否被限流）
$testStart = Get-Date
$rateLimited = $false
for ($i = 0; $i -lt 10; $i++) {
    try {
        Invoke-WebRequest -Uri $detailUrl -UseBasicParsing -SkipCertificateCheck -TimeoutSec 10 | Out-Null
    } catch {
        if ($_.Exception.Message -match '429|rate|limit|too many|502|503') {
            $rateLimited = $true
            Write-Host "第 $($i+1) 次请求被限流或返回服务器错误"
            break
        }
    }
    Start-Sleep -Milliseconds 200
}
$elapsed = ((Get-Date) - $testStart).TotalSeconds
Write-Host "10 次请求耗时: ${elapsed}s, 限流: $rateLimited"
```

### 步骤 10：检查是否有 CSV/API 导出

```powershell
# 查找页面中的导出链接
$exportLinks = [regex]::Matches($listHtml, 'href="([^"]*(?:export|csv|download|api)[^"]*)"', [IgnoreCase])
foreach ($link in $exportLinks) {
    Write-Host "发现导出链接: $($link.Groups[1].Value)"
}

# 尝试常见 CSV 导出 URL 模式
$csvPatterns = @(
    "$listUrl`?type=csv",
    "$listUrl`?format=csv",
    "$listUrl`?export=csv"
)
foreach ($csvUrl in $csvPatterns) {
    try {
        $resp = Invoke-WebRequest -Uri $csvUrl -UseBasicParsing -SkipCertificateCheck -TimeoutSec 10
        if ($resp.Headers['Content-Type'] -match 'csv|text') {
            Write-Host "CSV 导出可用: $csvUrl"
        }
    } catch {}
}
```

## 探测检查清单

- [ ] 列表页 URL 和总记录数
- [ ] **站点类型判定（学术导航站/图书馆列表站/其他）**
- [ ] **字段上限（最多可采集字段数）**
- [ ] 详情页是否存在
- [ ] 分页机制（URL 参数、每页条数）
- [ ] 列表页可采集字段清单
- [ ] 详情页可采集字段清单（如有）
- [ ] 字段是否来自 JavaScript 调用
- [ ] 列表页字段是否截断（描述等长文本，仅学术导航站）
- [ ] 反爬措施（Cloudflare、限流、登录墙、502 错误）
- [ ] SSL/证书问题（HTTP 或 HTTPS）
- [ ] 是否有 CSV/API 导出
- [ ] 代理是否需要（目标站是否限制地区访问）

## 输出格式

探测完成后，整理为结构化的探测报告：

```markdown
## 站点探测报告

### 基本信息
- 站点名称：XXX
- 站点类型：学术导航站 / 图书馆列表站 / 其他
- 列表页 URL：XXX
- 总记录数：XXX 条
- 字段上限：XX 个
- 每页条数：XX 条
- 分页参数：?p={page}&size={size}

### 详情页
- 是否存在：是/否
- URL 模式：https://xxx/resources/{id}（仅学术导航站）
- 需要请求详情页：是/否（列表页字段是否足够）

### 字段枚举
| 字段名 | 说明 | 采集来源 | 填充率 | 提取方式 |
|--------|------|----------|--------|---------|
| name | 数据库名称 | 列表页 | 100% | HTML 文本 |
| resourceId | 资源ID | 列表页 JS | 100% | collect() 函数 |
| ... | ... | ... | ... | ... |

### 反爬措施
- Cloudflare：否
- 限流：无 / 502 间歇性错误
- 登录墙：无
- robots.txt：无限制
- SSL/证书：需 HTTP（HTTPS 不可用）

### 导出
- CSV 导出：有/无
- API：有/无

### 建议并发数
- 学术导航站：推荐 5 并发
- 图书馆列表站：顺序请求，页间 300ms 延迟