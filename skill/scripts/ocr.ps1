param([Parameter(Mandatory=$true)][string]$List, [Parameter(Mandatory=$true)][string]$Out)
# Windows.Media.Ocr: reads every image path in $List, writes JSON [{path, lines:[{text,y}]}] to $Out.
# y = vertical centre of the line as a fraction of image height (0 = top).
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) {
    $task = $asTaskGeneric.MakeGenericMethod($t).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}
[Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType=WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime] | Out-Null

$engine = $null
foreach ($tag in @('en-US', 'en-GB', 'en')) {
    if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported([Windows.Globalization.Language]::new($tag))) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($tag)); break
    }
}
if (-not $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }

$results = New-Object System.Collections.Generic.List[object]
foreach ($p in [IO.File]::ReadAllLines($List, [Text.Encoding]::UTF8)) {
    [string]$path = $p.Trim([char]0xFEFF).Trim()
    if (-not $path) { continue }
    try {
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $h = [double]$dec.PixelHeight
        $bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $res = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
        $lines = @()
        foreach ($l in $res.Lines) {
            $ys = @($l.Words | ForEach-Object { $_.BoundingRect.Y + $_.BoundingRect.Height / 2 })
            $y = if ($ys.Count) { ($ys | Measure-Object -Average).Average / $h } else { 0 }
            $lines += [pscustomobject]@{ text = $l.Text; y = [math]::Round($y, 4) }
        }
        $stream.Dispose()
        $results.Add([pscustomobject]@{ path = $path; lang = $engine.RecognizerLanguage.LanguageTag; lines = $lines })
    } catch {
        $results.Add([pscustomobject]@{ path = $path; error = $_.Exception.Message; lines = @() })
    }
}
[IO.File]::WriteAllText($Out, (ConvertTo-Json -InputObject $results.ToArray() -Depth 5), (New-Object Text.UTF8Encoding $false))
