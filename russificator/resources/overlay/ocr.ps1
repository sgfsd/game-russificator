# Распознавание текста Windows (Windows.Media.Ocr) для оверлея «Русификатора игр».
# Запускается один раз и работает, пока открыт оверлей: читает из stdin строки
# «путь|ширина|высота» (сырые пиксели BGRA), пишет в stdout JSON со строками текста.
# Только чтение файлов кадров во временной папке программы — ничего больше.
param([string]$Lang = "en")
$ErrorActionPreference = "Stop"
# потоки UTF-8 явно: путь к кадру может быть с кириллицей (имя пользователя), консоли у процесса нет
$utf8 = New-Object System.Text.UTF8Encoding $false
$stdin = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), $utf8)
$stdout = New-Object System.IO.StreamWriter([Console]::OpenStandardOutput(), $utf8)
$stdout.AutoFlush = $true

function Out-Json($obj) { $stdout.WriteLine((ConvertTo-Json -Compress -Depth 4 $obj)) }

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics, ContentType = WindowsRuntime]
    $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
    $asTask = $asTask.MakeGenericMethod([Windows.Media.Ocr.OcrResult])
    $langs = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages)
    $pick = $langs | Where-Object { $_.LanguageTag -like "$Lang*" } | Select-Object -First 1
    if (-not $pick) {
        Out-Json @{ error = "no_language"; available = @($langs | ForEach-Object { $_.LanguageTag }) }
        exit 2
    }
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($pick)
    if (-not $engine) { Out-Json @{ error = "no_language" }; exit 2 }
    Out-Json @{ ready = $true; lang = $pick.LanguageTag; max = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension }
} catch {
    Out-Json @{ error = "winrt"; message = [string]$_.Exception.Message }
    exit 3
}

while ($true) {
    $req = $stdin.ReadLine()
    if ($req -eq $null -or $req -eq "quit") { break }
    try {
        $p = $req.Split("|")
        $bytes = [System.IO.File]::ReadAllBytes($p[0])
        $buf = [System.Runtime.InteropServices.WindowsRuntime.WindowsRuntimeBufferExtensions]::AsBuffer($bytes)
        $bmp = [Windows.Graphics.Imaging.SoftwareBitmap]::CreateCopyFromBuffer($buf,
            [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [int]$p[1], [int]$p[2],
            [Windows.Graphics.Imaging.BitmapAlphaMode]::Ignore)
        $task = $asTask.Invoke($null, @($engine.RecognizeAsync($bmp)))
        $null = $task.Wait(15000)
        $res = $task.Result
        $lines = New-Object System.Collections.Generic.List[object]
        foreach ($l in $res.Lines) {
            $x0 = [double]::MaxValue; $y0 = [double]::MaxValue; $x1 = 0.0; $y1 = 0.0
            foreach ($w in $l.Words) {
                $r = $w.BoundingRect
                if ($r.X -lt $x0) { $x0 = $r.X }; if ($r.Y -lt $y0) { $y0 = $r.Y }
                if ($r.X + $r.Width -gt $x1) { $x1 = $r.X + $r.Width }
                if ($r.Y + $r.Height -gt $y1) { $y1 = $r.Y + $r.Height }
            }
            if ($x1 -gt 0) {
                $lines.Add(@{ t = $l.Text; x = [int]$x0; y = [int]$y0; w = [int]($x1 - $x0); h = [int]($y1 - $y0) })
            }
        }
        $bmp.Dispose()
        Out-Json @{ lines = $lines.ToArray() }
    } catch {
        Out-Json @{ error = "recognize"; message = [string]$_.Exception.Message }
    }
}
