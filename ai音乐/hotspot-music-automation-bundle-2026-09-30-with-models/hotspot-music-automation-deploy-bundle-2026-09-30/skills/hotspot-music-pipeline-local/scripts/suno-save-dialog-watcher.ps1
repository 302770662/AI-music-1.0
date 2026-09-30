param(
    [int]$PollMilliseconds = 500,
    [switch]$Once
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms

if (-not ('SunoSaveDialog.NativeMethods' -as [type])) {
    Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
namespace SunoSaveDialog {
    public static class NativeMethods {
        public delegate bool EnumProc(IntPtr h, IntPtr l);
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int count);
        [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc callback, IntPtr data);
        [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr parent, EnumProc callback, IntPtr data);
        [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
    }
}
'@
}

$BM_CLICK = 0x00F5

$root = [System.Windows.Automation.AutomationElement]::RootElement
$windowCondition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Window
)

function Find-SaveDialog {
    $windows = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        $windowCondition
    )
    foreach ($window in $windows) {
        $name = [string]$window.Current.Name
        $class = [string]$window.Current.ClassName
        if (($name -match '(另存为|Save As)') -or
            ($class -match '^(#32770|Chrome_WidgetWin_0|Chrome_WidgetWin_1)$' -and $name -match '(cloudmusic|\.mp3|\.mp4|Suno|Save|保存)')) {
            return $window
        }
    }
    return $null
}

function Invoke-SaveButton($dialog) {
    $buttonCondition = New-Object System.Windows.Automation.AndCondition(
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button
        )),
        (New-Object System.Windows.Automation.OrCondition(
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty,
                '保存'
            )),
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty,
                '保存(S)'
            )),
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty,
                'Save'
            ))
        ))
    )
    $button = $dialog.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $buttonCondition)
    if ($null -eq $button) { return $false }
    $pattern = $null
    if ($button.TryGetCurrentPattern(
        [System.Windows.Automation.InvokePattern]::Pattern,
        [ref]$pattern
    )) {
        ([System.Windows.Automation.InvokePattern]$pattern).Invoke()
        return $true
    }
    return $false
}

function Invoke-ForegroundSaveFallback {
    $handle = [SunoSaveDialog.NativeMethods]::GetForegroundWindow()
    if ($handle -eq [IntPtr]::Zero) { return $false }
    $title = New-Object System.Text.StringBuilder 512
    [void][SunoSaveDialog.NativeMethods]::GetWindowText($handle, $title, $title.Capacity)
    $caption = $title.ToString()
    if ($caption -notmatch '^(另存为|Save As)(\s|-|$)') { return $false }
    [void][SunoSaveDialog.NativeMethods]::SetForegroundWindow($handle)
    Start-Sleep -Milliseconds 100
    [System.Windows.Forms.SendKeys]::SendWait('%s')
    Start-Sleep -Milliseconds 150
    return $true
}

function Invoke-Win32SaveFallback {
    $dialogHandles = [System.Collections.Generic.List[IntPtr]]::new()
    $topCallback = [SunoSaveDialog.NativeMethods+EnumProc]{
        param($handle, $data)
        if (-not [SunoSaveDialog.NativeMethods]::IsWindowVisible($handle)) { return $true }
        $title = New-Object System.Text.StringBuilder 512
        [void][SunoSaveDialog.NativeMethods]::GetWindowText($handle, $title, $title.Capacity)
        if ($title.ToString() -match '^(另存为|Save As)(\s|-|$)') { [void]$dialogHandles.Add($handle) }
        return $true
    }
    [void][SunoSaveDialog.NativeMethods]::EnumWindows($topCallback, [IntPtr]::Zero)
    foreach ($dialogHandle in $dialogHandles) {
        $buttonHandles = [System.Collections.Generic.List[IntPtr]]::new()
        $childCallback = [SunoSaveDialog.NativeMethods+EnumProc]{
            param($handle, $data)
            $class = New-Object System.Text.StringBuilder 128
            $title = New-Object System.Text.StringBuilder 256
            [void][SunoSaveDialog.NativeMethods]::GetClassName($handle, $class, $class.Capacity)
            [void][SunoSaveDialog.NativeMethods]::GetWindowText($handle, $title, $title.Capacity)
            if ($class.ToString() -eq 'Button' -and $title.ToString() -match '^(保存|保存\(S\)|Save)$') {
                [void]$buttonHandles.Add($handle)
            }
            return $true
        }
        [void][SunoSaveDialog.NativeMethods]::EnumChildWindows($dialogHandle, $childCallback, [IntPtr]::Zero)
        foreach ($buttonHandle in $buttonHandles) {
            [void][SunoSaveDialog.NativeMethods]::SendMessage($buttonHandle, $BM_CLICK, [IntPtr]::Zero, [IntPtr]::Zero)
            return $true
        }
    }
    return $false
}

do {
    $dialog = Find-SaveDialog
    if ($null -ne $dialog) {
        try { [void](Invoke-SaveButton $dialog) } catch { }
    }
    try { [void](Invoke-ForegroundSaveFallback) } catch { }
    try { [void](Invoke-Win32SaveFallback) } catch { }
    if ($Once) { break }
    Start-Sleep -Milliseconds ([Math]::Max(100, $PollMilliseconds))
} while ($true)
