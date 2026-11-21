param(
    [string] $WebViewPackage,
    [string] $BuildDirectory,
    [string] $OutputDirectory,
    [string] $Name = 'Agent Usage Desk Windows'
)

$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$version = '1.0.4314.35'
if (!$BuildDirectory) { $BuildDirectory = Join-Path $project 'build-windows' }
if (!$OutputDirectory) { $OutputDirectory = Join-Path $project 'dist' }
$BuildDirectory = [IO.Path]::GetFullPath($BuildDirectory)
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
if (!$WebViewPackage) {
    $WebViewPackage = Join-Path $BuildDirectory "webview2-$version.nupkg"
    if (!(Test-Path -LiteralPath $WebViewPackage)) {
        New-Item -ItemType Directory -Path $BuildDirectory -Force | Out-Null
        $uri = "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/$version/microsoft.web.webview2.$version.nupkg"
        Invoke-WebRequest -UseBasicParsing -Uri $uri -OutFile $WebViewPackage
    }
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$hostBuild = Join-Path $BuildDirectory 'host'
New-Item -ItemType Directory -Path $hostBuild -Force | Out-Null
$selected = @{
    'lib/net462/Microsoft.Web.WebView2.Core.dll' = 'Microsoft.Web.WebView2.Core.dll'
    'lib/net462/Microsoft.Web.WebView2.WinForms.dll' = 'Microsoft.Web.WebView2.WinForms.dll'
    'runtimes/win-x64/native/WebView2Loader.dll' = 'WebView2Loader.dll'
    'LICENSE.txt' = 'WebView2-LICENSE.txt'
}
$archive = [IO.Compression.ZipFile]::OpenRead((Resolve-Path -LiteralPath $WebViewPackage).Path)
try {
    foreach ($path in $selected.Keys) {
        $entry = $archive.GetEntry($path)
        if (!$entry) { throw "Required WebView2 package file missing: $path" }
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $hostBuild $selected[$path]), $true)
    }
}
finally { $archive.Dispose() }

$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$hostExe = Join-Path $hostBuild 'DeskWindow.exe'
$hostSource = Join-Path $project 'DeskWindow.cs'
$hostManifest = Join-Path $project 'DeskWindow.manifest'
$iconPath = Join-Path $project 'desk.ico'
$core = Join-Path $hostBuild 'Microsoft.Web.WebView2.Core.dll'
$forms = Join-Path $hostBuild 'Microsoft.Web.WebView2.WinForms.dll'
$compilerArguments = @(
    '/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/codepage:65001',
    "/out:$hostExe", "/win32icon:$iconPath", "/win32manifest:$hostManifest",
    '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll', '/reference:System.Net.Http.dll',
    "/reference:$core", "/reference:$forms", $hostSource
)
& $compiler @compilerArguments
if ($LASTEXITCODE -ne 0) { throw "Windows host compilation failed: $LASTEXITCODE" }
Copy-Item -LiteralPath (Join-Path $project 'DeskWindow.exe.config') -Destination $hostBuild -Force

$packageArguments = @(
    '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed', '--name', $Name,
    '--icon', "$project/desk.ico", '--add-data', "$project/ui;ui",
    '--add-data', "$project/THIRD_PARTY.txt;.", '--add-data', "$hostBuild;host",
    '--specpath', "$BuildDirectory/spec", '--workpath', "$BuildDirectory/pack", '--distpath', $OutputDirectory,
    "$project/app.py"
)
& python @packageArguments
if ($LASTEXITCODE -ne 0) { throw "Packaging failed: $LASTEXITCODE" }
