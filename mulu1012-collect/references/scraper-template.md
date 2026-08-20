# 采集脚本模板

## 概述

通用采集脚本模板，适用于任何详情页批量采集场景。基于 PowerShell RunspacePool 实现并发，支持断点续传和错误重试。

## 脚本模板

```powershell
<#
.SYNOPSIS
    通用数据库导航站详情页采集脚本
.DESCRIPTION
    从输入 CSV（含 URL 列表）逐条抓取详情页，提取字段，输出 TSV
.PARAMETER InputCsv
    输入 CSV 路径（含 URL 列表）
.PARAMETER OutputTsv
    输出 TSV 路径
.PARAMETER Concurrency
    并发连接数（默认 5）
.PARAMETER StartIndex
    从第几条开始（默认 0）
.PARAMETER Count
    采集多少条（0 = 全部）
.PARAMETER Proxy
    代理地址（空 = 直连，依赖 TUN 模式）
#>

param(
    [Parameter(Mandatory=$true)]
    [string]$InputCsv,
    [string]$OutputTsv = "output.tsv",
    [int]$Concurrency = 5,
    [int]$StartIndex = 0,
    [int]$Count = 0,
    [string]$Proxy = ""
)

$ErrorActionPreference = "Continue"

# ========== 配置区 ==========

# 进度文件路径
$progressFile = [System.IO.Path]::ChangeExtension($OutputTsv, ".progress.json")

# HTTP 请求参数
$webParams = @{
    UseBasicParsing = $true
    TimeoutSec = 30
    SkipCertificateCheck = $true
}
if ($Proxy -ne "") {
    $webParams.Proxy = $Proxy
    Write-Host "Using proxy: $Proxy"
}

# URL 列名（CSV 中的列名）
$urlColumnName = "Frontdoor-URL"  # ← 改为目标站的 URL 列名

# ID 列名（CSV 中的列名）
$idColumnName = "DBIS-Ressource_ID"  # ← 改为目标站的 ID 列名

# 输出字段定义
# ← 根据字段推荐表修改此数组
$headers = @(
    'ID',
    'Titel',
    'URL',
    # ... 根据探测结果添加字段
    'Status',
    'Error'
)

# ========== 配置区结束 ==========

# 读取输入
$records = Import-Csv $InputCsv -Encoding UTF8
Write-Host "Total records: $($records.Count)"

# 范围过滤
if ($StartIndex -gt 0) { $records = $records | Select-Object -Skip $StartIndex }
if ($Count -gt 0) { $records = $records | Select-Object -First $Count }

# 加载进度
$completedIds = @{}
if (Test-Path $progressFile) {
    try {
        $progress = Get-Content $progressFile -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($id in $progress.completed) { $completedIds[$id] = $true }
        Write-Host "Already completed: $($completedIds.Count) records"
    } catch { Write-Warning "Could not load progress: $($_.Exception.Message)" }
}

# 初始化输出文件
if (!(Test-Path $OutputTsv)) {
    $headers -join "`t" | Out-File -FilePath $OutputTsv -Encoding UTF8
}

$lockObj = [System.Object]::new()

# 文本清理函数
function CleanText {
    param([string]$text)
    if (!$text) { return '' }
    $text = [regex]::Replace($text, '<[^>]+>', '')
    $text = [System.Net.WebUtility]::HtmlDecode($text)
    $text = [regex]::Replace($text, '\s+', ' ')
    return $text.Trim()
}

# Worker 脚本块
$workerScript = {
    param($record, $webParams, $headers, $outputTsv, $lockObj, $urlColumnName, $idColumnName)

    $resId = $record.$idColumnName
    $url = $record.$urlColumnName

    # 初始化行数据
    $row = [ordered]@{}
    foreach ($h in $headers) { $row[$h] = '' }
    $row['ID'] = $resId
    $row['Status'] = 'OK'
    $row['Error'] = ''

    # ============ 字段提取区 ============
    # ← 根据探测结果编写每个字段的提取逻辑
    # 以下为示例（DBIS 模式）：

    try {
        # 带重试的 HTTP 请求
        $maxRetries = 3
        $html = $null
        for ($attempt = 1; $attempt -le $maxRetries; $attempt++) {
            try {
                $response = Invoke-WebRequest -Uri $url @webParams
                $html = $response.Content
                break
            } catch {
                if ($attempt -lt $maxRetries) {
                    Start-Sleep -Milliseconds ($attempt * 1000)
                } else { throw }
            }
        }
        if (!$html) { throw "Failed after $maxRetries attempts" }

        # === 提取各字段 ===
        # 示例：标题
        $titleMatch = [regex]::Match($html, '<h1[^>]*>(.*?)</h1>', [Singleline])
        if ($titleMatch.Success) {
            $row['Titel'] = [System.Net.WebUtility]::HtmlDecode(
                ([regex]::Replace($titleMatch.Groups[1].Value, '<[^>]+>', '')).Trim()
            )
        }

        # 示例：描述
        $descMatch = [regex]::Match($html, 'class="description[^"]*"[^>]*>(.*?)</div>', [Singleline])
        if ($descMatch.Success) {
            $desc = $descMatch.Groups[1].Value
            $desc = [regex]::Replace($desc, '<br\s*/?>', ' ')
            $desc = [regex]::Replace($desc, '<[^>]+>', '')
            $row['Beschreibung'] = [System.Net.WebUtility]::HtmlDecode($desc).Trim()
        }

        # ... 更多字段提取 ...

    } catch {
        $row['Status'] = 'ERROR'
        $row['Error'] = $_.Exception.Message
    }

    # ============ 字段提取区结束 ============

    # 线程安全写入
    $lineValues = @()
    foreach ($key in $headers) {
        $val = $row[$key]
        if ($null -eq $val) { $val = '' }
        $val = $val -replace "`t", ' '
        $val = $val -replace "`r`n", ' '
        $val = $val -replace "`n", ' '
        $lineValues += $val
    }
    $line = $lineValues -join "`t"

    [System.Threading.Monitor]::Enter($lockObj)
    try {
        $line | Out-File -FilePath $outputTsv -Encoding UTF8 -Append
    } finally {
        [System.Threading.Monitor]::Exit($lockObj)
    }

    return [PSCustomObject]@{ Id = $resId; Status = $row['Status'] }
}

# 过滤未完成记录
$toProcess = $records | Where-Object { -not $completedIds.ContainsKey($_.$idColumnName) }
Write-Host "Records to process: $($toProcess.Count)"

if ($toProcess.Count -eq 0) {
    Write-Host "All records already completed!"
    return
}

# 创建并发池
$sessionState = [System.Management.Automation.Runspaces.InitialSessionState]::CreateDefault()
$pool = [RunspaceFactory]::CreateRunspacePool(1, $Concurrency, $sessionState, $Host)
$pool.Open()

$activeJobs = [System.Collections.ArrayList]::new()
$queue = [System.Collections.Queue]::new($toProcess)
$batchProcessed = 0; $batchSuccess = 0; $batchError = 0
$startTime = Get-Date

Write-Host "Starting scraping with $Concurrency concurrent workers..."
Write-Host "Output: $OutputTsv"

while ($queue.Count -gt 0 -or $activeJobs.Count -gt 0) {
    # 填充并发任务
    while ($activeJobs.Count -lt $Concurrency -and $queue.Count -gt 0) {
        $record = $queue.Dequeue()
        $ps = [PowerShell]::Create()
        $ps.RunspacePool = $pool
        [void]$ps.AddScript($workerScript)
        [void]$ps.AddParameter("record", $record)
        [void]$ps.AddParameter("webParams", $webParams)
        [void]$ps.AddParameter("headers", $headers)
        [void]$ps.AddParameter("outputTsv", $outputTsv)
        [void]$ps.AddParameter("lockObj", $lockObj)
        [void]$ps.AddParameter("urlColumnName", $urlColumnName)
        [void]$ps.AddParameter("idColumnName", $idColumnName)
        $handle = $ps.BeginInvoke()
        [void]$activeJobs.Add([PSCustomObject]@{ PS=$ps; Handle=$handle; Record=$record })
    }

    # 收集完成的任务
    $completed = @()
    foreach ($job in $activeJobs) {
        if ($job.Handle.IsCompleted) { $completed += $job }
    }

    foreach ($job in $completed) {
        try {
            $result = $job.PS.EndInvoke($job.Handle)
            if ($result) {
                foreach ($r in $result) {
                    $batchProcessed++
                    $completedIds[$r.Id] = $true
                    if ($r.Status -eq 'OK') { $batchSuccess++ } else { $batchError++ }
                }
            }
        } catch {
            $batchError++; $batchProcessed++
            $completedIds[$job.Record.$idColumnName] = $true
        }
        $job.PS.Dispose()
        $activeJobs.Remove($job) | Out-Null
    }

    # 进度输出
    if ($batchProcessed % 10 -eq 0 -and $batchProcessed -gt 0) {
        $elapsed = (Get-Date) - $startTime
        $rate = if ($elapsed.TotalSeconds -gt 0) { [math]::Round($batchProcessed / $elapsed.TotalSeconds, 2) } else { 0 }
        $remaining = $toProcess.Count - $batchProcessed
        $eta = if ($rate -gt 0) { [math]::Round($remaining / $rate / 60, 1) } else { '?' }
        Write-Host "[$batchProcessed/$($toProcess.Count)] OK:$batchSuccess ERR:$batchError Rate:${rate}/s ETA:${eta}min [$(Get-Date -Format 'HH:mm:ss')]"
    }

    # 保存进度
    if ($batchProcessed % 100 -eq 0 -and $batchProcessed -gt 0) {
        $progressObj = [PSCustomObject]@{
            completed = $completedIds.Keys | Sort-Object
            timestamp = (Get-Date).ToString('o')
            processed = $batchProcessed
            success = $batchSuccess
            errors = $batchError
        }
        $progressObj | ConvertTo-Json -Depth 5 | Set-Content -Path $progressFile -Encoding UTF8
    }

    if ($activeJobs.Count -ge $Concurrency) { Start-Sleep -Milliseconds 100 }
}

# 最终保存
$progressObj = [PSCustomObject]@{
    completed = $completedIds.Keys | Sort-Object
    timestamp = (Get-Date).ToString('o')
    processed = $batchProcessed
    success = $batchSuccess
    errors = $batchError
}
$progressObj | ConvertTo-Json -Depth 5 | Set-Content -Path $progressFile -Encoding UTF8

$pool.Close(); $pool.Dispose()

$elapsed = (Get-Date) - $startTime
Write-Host "`n=== SCRAPING COMPLETE ==="
Write-Host "Processed: $batchProcessed"
Write-Host "Success: $batchSuccess"
Write-Host "Errors: $batchError"
Write-Host "Elapsed: $([math]::Round($elapsed.TotalMinutes, 1)) minutes"
Write-Host "Output: $OutputTsv"
```

## 适配新站点的修改要点

使用此模板适配新站点时，需要修改以下部分：

| 修改项 | 位置 | 说明 |
|--------|------|------|
| `$urlColumnName` | 配置区 | CSV 中的 URL 列名 |
| `$idColumnName` | 配置区 | CSV 中的 ID 列名 |
| `$headers` | 配置区 | 输出字段列表 |
| 字段提取区 | worker 脚本块 | 每个字段的正则表达式 |
| 清理逻辑 | worker 脚本块 | 站点特有的 HTML 结构处理 |

## 并发数建议

**默认并发 1（顺序请求）。** 图书馆门户普遍扛不住并发——国图并发就间歇吐 502。只有实测确认站点不限流才往上调：

| 情形 | 并发 | 说明 |
|---|---|---|
| **默认 / 图书馆门户** | **1** | 顺序请求 + 递增退避，最稳 |
| 测试阶段 | 1 | 先用 5 条验证字段提取 |
| 实测无限流的大型学术站 | 3–5 | 必须先小批量试探，出现 5xx 立刻降回 1 |
