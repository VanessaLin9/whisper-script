// Compile with -D DESKTOP_UI_TEST alongside MeetingDesk.swift. Uses synthetic
// data only, never starts the backend or reads a user's meeting workspace.
import AppKit
import SwiftUI

@main
struct EditorSnapshot {
    static func main() {
        let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let desk = Desk()
        let cues = [
            SubtitleCue(["id": 0, "number": "1", "time": "00:00:01,250 --> 00:00:04,500", "text": "今天先確認 Athena API 的測試結果。"]),
            SubtitleCue(["id": 1, "number": "2", "time": "00:00:05,000 --> 00:00:08,990", "text": "這個版本已修正連線問題，下一步驗證錯誤重試。"]),
            SubtitleCue(["id": 2, "number": "3", "time": "00:00:09,500 --> 00:00:13,000", "text": "專有名詞請保留英文，其餘使用繁體中文。"]),
        ]
        let text = cues.map(\.text).joined(separator: "\n\n")
        let views: [(String, AnyView, NSSize)] = [
            ("text-editor", AnyView(TranscriptEditor(desk: desk, draft: EditDraft(folder: "/fixture", title: "AI 團隊週會", kind: "prepared", token: "fixture", text: text, cues: []))), NSSize(width: 848, height: 668)),
            ("srt-editor", AnyView(TranscriptEditor(desk: desk, draft: EditDraft(folder: "/fixture", title: "AI 團隊週會", kind: "srt", token: "fixture", text: "", cues: cues))), NSSize(width: 848, height: 668)),
            ("rename", AnyView(RenameMeetingView(desk: desk, draft: RenameDraft(folder: "/fixture", title: "語音備忘錄 57"))), NSSize(width: 536, height: 270)),
        ]
        var windows: [NSWindow] = []
        for (name, root, size) in views {
            let view = NSHostingView(rootView: root.environment(\.colorScheme, .light))
            let window = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.titled], backing: .buffered, defer: false)
            window.title = name
            window.appearance = NSAppearance(named: .aqua)
            window.contentView = view
            window.center()
            window.orderFront(nil)
            windows.append(window)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
            for (index, window) in windows.enumerated() {
                guard let view = window.contentView, let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { exit(1) }
                view.cacheDisplay(in: view.bounds, to: bitmap)
                guard let data = bitmap.representation(using: .png, properties: [:]) else { exit(1) }
                do { try data.write(to: output.appendingPathComponent(views[index].0 + ".png")) }
                catch { print(error); exit(1) }
            }
            app.terminate(nil)
        }
        app.run()
    }
}
