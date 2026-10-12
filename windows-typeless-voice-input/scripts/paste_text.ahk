#Requires AutoHotkey v2.0
#SingleInstance Force

if A_Args.Length < 1 {
    FileAppend "Usage: paste_text.ahk <utf8-text-file> [paste-delay-ms] [restore-delay-ms]`n", "*"
    ExitApp 2
}

textPath := A_Args[1]
pasteDelayMs := A_Args.Length >= 2 ? Integer(A_Args[2]) : 120
restoreDelayMs := A_Args.Length >= 3 ? Integer(A_Args[3]) : 800

if !FileExist(textPath) {
    FileAppend "Text file not found.`n", "*"
    ExitApp 3
}

savedClipboard := ClipboardAll()
try {
    A_Clipboard := FileRead(textPath, "UTF-8")
    if !ClipWait(2) {
        ExitApp 4
    }
    Sleep pasteDelayMs
    Send "^v"
    Sleep restoreDelayMs
} finally {
    A_Clipboard := savedClipboard
}

ExitApp 0
