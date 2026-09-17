import SwiftUI
import AppKit
import UniformTypeIdentifiers

struct Meeting: Identifiable {
    let id: String
    let title: String
    let date: String
    let status: String
    let error: String
    let profile: String
    let raw: Bool
    let prepared: Bool
    let corrected: Bool
    let cleaned: Bool
    let reviewed: Bool
    init(_ value: [String: Any]) {
        id = value["id"] as? String ?? ""
        title = value["title"] as? String ?? ""
        date = value["date"] as? String ?? ""
        status = value["status"] as? String ?? ""
        error = value["error"] as? String ?? ""
        profile = value["profile"] as? String ?? ""
        raw = value["raw"] as? Bool ?? false
        prepared = value["prepared"] as? Bool ?? false
        corrected = value["corrected"] as? Bool ?? false
        cleaned = value["cleaned"] as? Bool ?? false
        reviewed = value["reviewed"] as? Bool ?? false
    }
}

struct SubtitleCue: Identifiable {
    let id: Int
    let number: String
    let time: String
    var text: String
    init(_ value: [String: Any]) {
        id = value["id"] as? Int ?? 0
        number = value["number"] as? String ?? ""
        time = value["time"] as? String ?? ""
        text = value["text"] as? String ?? ""
    }
}

struct EditDraft: Identifiable {
    let id = UUID()
    let folder: String
    let title: String
    let kind: String
    let token: String
    let text: String
    let cues: [SubtitleCue]
}

struct RenameDraft: Identifiable {
    let id = UUID()
    let folder: String
    let title: String
}

final class Desk: ObservableObject {
    @Published var meetings: [Meeting] = []
    @Published var selected: String?
    @Published var busy = false
    @Published var processing = false
    @Published var progress = ""
    @Published var notice = ""
    @Published var error: String?
    @Published var settings: [String: Any] = [:]
    @Published var checks: [[String: Any]] = []
    @Published var profiles: [[String: Any]] = []
    @Published var profile = ""
    @Published var preview = ""
    @Published var previewPath = ""
    @Published var previewKind = "prepared"
    @Published var previewToken = ""
    @Published var previewEditable = false
    @Published var previewEditError = ""
    @Published var previewCues: [SubtitleCue] = []
    @Published var editor: EditDraft?
    @Published var renameDraft: RenameDraft?
    @Published var hasUnsavedEdits = false
    @Published var importDraft: [String: String]?
    @Published var showSettings = false
    @Published var showReview = false
    var active: Process?
    var meeting: Meeting? { meetings.first { $0.id == selected } }
    var repo: String { Bundle.main.object(forInfoDictionaryKey: "MeetingRepo") as? String ?? FileManager.default.currentDirectoryPath }
    var python: String { Bundle.main.object(forInfoDictionaryKey: "MeetingPython") as? String ?? "/usr/bin/python3" }

    func call(_ request: [String: Any], failed: ((String) -> Void)? = nil, done: @escaping ([String: Any]) -> Void) {
        guard !busy else { return }
        busy = true
        let process = Process()
        let input = Pipe(), output = Pipe(), errors = Pipe()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-m", "src.desktop"]
        process.currentDirectoryURL = URL(fileURLWithPath: repo)
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = repo
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (environment["PATH"] ?? "/usr/bin:/bin")
        process.environment = environment
        process.standardInput = input
        process.standardOutput = output
        process.standardError = errors
        active = process
        errors.fileHandleForReading.readabilityHandler = { handle in _ = handle.availableData }
        do {
            try process.run()
            input.fileHandleForWriting.write(try JSONSerialization.data(withJSONObject: request))
            try input.fileHandleForWriting.close()
        } catch {
            if let failed = failed { failed(error.localizedDescription) } else { self.error = error.localizedDescription }
            busy = false
            processing = false
            active = nil
            errors.fileHandleForReading.readabilityHandler = nil
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            var buffer = Data()
            var result: [String: Any]?
            while true {
                let data = output.fileHandleForReading.availableData
                if data.isEmpty { break }
                buffer.append(data)
                while let newline = buffer.firstIndex(of: 10) {
                    let line = buffer.prefix(upTo: newline)
                    buffer.removeSubrange(...newline)
                    guard let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
                    if event["type"] as? String == "result" { result = event }
                    if event["type"] as? String == "progress" {
                        let stage = event["stage"] as? String ?? ""
                        let labels = ["validate_input": "檢查音訊", "check_outputs": "檢查輸出", "normalize": "準備音訊", "transcribe": "Whisper 正在轉錄", "validate_artifacts": "檢查逐字稿", "cleanup": "整理產物", "preclean": "整理逐字稿段落"]
                        let label = labels[stage] ?? stage
                        DispatchQueue.main.async { self.progress = label }
                    }
                }
            }
            process.waitUntilExit()
            errors.fileHandleForReading.readabilityHandler = nil
            let final = result
            DispatchQueue.main.async {
                self.busy = false
                self.active = nil
                self.processing = false
                if let value = final, value["ok"] as? Bool == true {
                    done(value)
                } else {
                    if final?["cancelled"] as? Bool == true {
                        self.notice = "已取消，音檔已保留。可以稍後繼續。"
                    } else {
                        let message = final?["error"] as? String ?? "背景程式未完成（\(process.terminationStatus)）。請重新開啟工具，或檢查 Python 路徑。"
                        if let failed = failed { failed(message) } else { self.error = message }
                    }
                    if request["action"] as? String == "process" { self.refresh() }
                }
            }
        }
    }

    func load() {
        call(["action": "environment"]) { value in
            self.settings = value["settings"] as? [String: Any] ?? [:]
            self.checks = value["checks"] as? [[String: Any]] ?? []
            self.profiles = value["profiles"] as? [[String: Any]] ?? []
            self.refresh()
        }
    }
    func refresh() {
        call(["action": "list"]) { value in
            self.meetings = (value["meetings"] as? [[String: Any]] ?? []).map(Meeting.init)
            if let warnings = value["warnings"] as? [String], !warnings.isEmpty { self.notice = warnings.joined(separator: "\n") }
            if let selected = self.selected, !self.meetings.contains(where: { $0.id == selected }) { self.selected = nil }
            self.readPreview()
        }
    }
    func readPreview() {
        previewEditable = false
        previewToken = ""
        previewCues = []
        previewEditError = ""
        guard let meeting = meeting else { preview = ""; previewPath = ""; return }
        call(["action": "preview", "folder": meeting.id, "kind": previewKind]) { value in
            self.preview = value["text"] as? String ?? ""
            self.previewPath = value["path"] as? String ?? ""
            self.previewToken = value["token"] as? String ?? ""
            self.previewEditable = value["editable"] as? Bool ?? false
            self.previewEditError = value["edit_error"] as? String ?? ""
            self.previewCues = (value["cues"] as? [[String: Any]] ?? []).map(SubtitleCue.init)
        }
    }
    func beginEdit() {
        guard !busy, let meeting = meeting, previewEditable, !previewToken.isEmpty else { return }
        editor = EditDraft(folder: meeting.id, title: meeting.title, kind: previewKind,
                           token: previewToken, text: preview, cues: previewCues)
    }
    func saveEdit(_ draft: EditDraft, text: String, cues: [SubtitleCue], failed: @escaping (String) -> Void) {
        var request: [String: Any] = ["action": "save_edit", "folder": draft.folder, "kind": draft.kind, "token": draft.token]
        if draft.kind == "srt" {
            request["cues"] = cues.map { ["id": $0.id, "text": $0.text] as [String: Any] }
        } else { request["text"] = text }
        call(request, failed: failed) { result in
            self.hasUnsavedEdits = false
            self.editor = nil
            self.previewKind = result["kind"] as? String ?? draft.kind
            let warning = result["warning"] as? String ?? ""
            self.notice = warning.isEmpty ? "訂正已儲存。原稿與先前版本保留在會議資料夾內。" : warning
            self.refresh()
        }
    }
    func chooseAudio() {
        let panel = NSOpenPanel()
        panel.title = "匯入語音備忘錄或音訊檔"
        panel.allowedContentTypes = [.audio, .movie]
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url { inspect(url) }
    }
    func inspect(_ url: URL) {
        guard !busy else { notice = "請等待目前操作完成，再匯入音檔。"; return }
        call(["action": "inspect", "path": url.path]) { value in
            self.importDraft = value.reduce(into: [:]) { dict, pair in if let text = pair.value as? String { dict[pair.key] = text } }
        }
    }
    func addAudio(_ draft: [String: String]) {
        importDraft = nil
        progress = "保存音檔"
        call(["action": "import", "path": draft["path"] ?? "", "title": draft["title"] ?? "", "meeting_time": draft["meeting_time"] ?? ""]) { value in
            if let meeting = value["meeting"] as? [String: Any] { self.selected = meeting["id"] as? String }
            self.notice = "音檔已保存，按「轉錄並整理」開始。"
            self.refresh()
        }
    }
    func run() {
        guard let selected = selected else { return }
        processing = true
        progress = "準備開始"
        notice = ""
        call(["action": "process", "folder": selected]) { _ in
            self.notice = "轉錄與預清洗完成。選擇提示詞，即可準備 LLM 交接。"
            self.previewKind = "prepared"
            self.refresh()
        }
    }
    func handoff(summary: Bool = false) {
        guard let selected = selected else { return }
        call(["action": "handoff", "folder": selected, "profile": profile, "summary": summary]) { value in
            if let text = value["text"] as? String {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
            }
            if let path = value["path"] as? String {
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
            }
            self.notice = "最新訂正內容與時間軸已放入交接包，並複製到剪貼簿。可貼上或把檔案拖給 LLM。"
        }
    }
    func importCleaned() {
        guard let selected = selected else { return }
        let panel = NSOpenPanel()
        panel.title = "匯入 LLM 回傳的完整清洗逐字稿"
        panel.allowedContentTypes = [.plainText]
        if panel.runModal() == .OK, let url = panel.url {
            call(["action": "import_cleaned", "folder": selected, "path": url.path]) { _ in
                self.previewKind = "cleaned"
                self.notice = "長度檢查通過。請對照預清洗稿，確認內容保真。"
                self.refresh()
            }
        }
    }
    func approve() {
        guard let selected = selected else { return }
        call(["action": "review", "folder": selected]) { _ in
            self.notice = "清洗稿已確認，可以準備會議記錄交接。"
            self.refresh()
        }
    }
    func reveal() {
        if let selected = selected { NSWorkspace.shared.open(URL(fileURLWithPath: selected)) }
    }
}

struct ContentView: View {
    @ObservedObject var desk: Desk
    @State private var search = ""
    @State private var targeted = false
    private let accent = Color(nsColor: NSColor(name: nil) { appearance in
        if appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua {
            return NSColor(red: 0.40, green: 0.77, blue: 0.66, alpha: 1)
        }
        return NSColor(red: 0.13, green: 0.43, blue: 0.38, alpha: 1)
    })
    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Divider()
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("讓每場討論，留下完整記錄。").font(.system(size: 23, weight: .semibold))
                        Text("本機轉錄  /  繁體中文與英文  /  \(desk.settings["model"] as? String ?? "medium")")
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button(action: desk.chooseAudio) { Label("匯入音檔", systemImage: "plus") }
                        .buttonStyle(.borderedProminent).tint(accent).disabled(desk.busy)
                }.padding(28)
                Divider()
                if let meeting = desk.meeting { detail(meeting) } else { welcome }
                if !desk.notice.isEmpty {
                    HStack(alignment: .top) {
                        Image(systemName: "info.circle")
                        Text(desk.notice).font(.system(size: 12)).textSelection(.enabled)
                        Spacer()
                        Button { desk.notice = "" } label: { Image(systemName: "xmark") }.buttonStyle(.plain)
                    }.padding(14).background(accent.opacity(0.08))
                }
                if desk.busy {
                    HStack {
                        ProgressView().controlSize(.small)
                        Text(desk.processing ? desk.progress : "處理中…").font(.system(size: 12))
                        Spacer()
                        if desk.processing { Button("取消轉錄") { desk.active?.terminate(); desk.progress = "正在停止並保留音檔" } }
                    }.padding(14)
                }
            }.background(Color(nsColor: .windowBackgroundColor))
        }
        .frame(minWidth: 1000, minHeight: 680)
        .tint(accent)
        .overlay {
            if targeted {
                RoundedRectangle(cornerRadius: 14).fill(accent.opacity(0.12))
                    .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(accent, style: StrokeStyle(lineWidth: 3, dash: [10])))
                    .overlay(Label("放開以匯入音檔", systemImage: "waveform.badge.plus").font(.largeTitle).padding(30).background(.regularMaterial).cornerRadius(18))
                    .padding(12).allowsHitTesting(false)
            }
        }
        .onDrop(of: [UTType.fileURL.identifier], isTargeted: $targeted) { providers in
            guard !desk.busy, let first = providers.first else { return false }
            first.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                var url: URL?
                if let data = item as? Data { url = URL(dataRepresentation: data, relativeTo: nil) }
                else if let value = item as? URL { url = value }
                if let url = url { DispatchQueue.main.async { desk.inspect(url) } }
            }
            return true
        }
        .onAppear { desk.load() }
        .onChange(of: desk.selected) {
            desk.profile = desk.meeting?.profile ?? ""
            if desk.meeting?.corrected == true { desk.previewKind = "corrected" }
            else if desk.previewKind == "corrected" { desk.previewKind = "prepared" }
            desk.readPreview()
        }
        .onChange(of: desk.previewKind) { desk.readPreview() }
        .sheet(isPresented: Binding(get: { desk.importDraft != nil }, set: { if !$0 { desk.importDraft = nil } })) {
            ImportView(desk: desk, draft: desk.importDraft ?? [:])
        }
        .sheet(isPresented: $desk.showSettings) { SettingsView(desk: desk) }
        .sheet(item: $desk.editor) { draft in TranscriptEditor(desk: desk, draft: draft) }
        .sheet(item: $desk.renameDraft) { draft in RenameMeetingView(desk: desk, draft: draft) }
        .alert("操作未完成", isPresented: Binding(get: { desk.error != nil }, set: { if !$0 { desk.error = nil } })) {
            Button("知道了", role: .cancel) { desk.error = nil }
        } message: { Text(desk.error ?? "") }
        .alert("確認清洗稿內容", isPresented: $desk.showReview) {
            Button("已對照確認") { desk.approve() }
            Button("繼續檢查", role: .cancel) {}
        } message: {
            Text("請先對照原始／預清洗稿：開頭與結尾、議題、實質問答、數字和技術細節仍完整，未新增摘要或捏造內容，不確定的詞彙有標示。長度檢查本身不能代替內容檢查。")
        }
    }
    var sidebar: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 11) {
                Image(systemName: "waveform").font(.system(size: 26, weight: .medium)).foregroundStyle(accent)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Meeting Desk").font(.system(size: 17, weight: .bold))
                    Text("你的本機會議工作台").font(.system(size: 10)).foregroundStyle(.secondary)
                }
            }.padding(.top, 10)
            TextField("搜尋會議", text: $search).textFieldStyle(.roundedBorder)
            HStack {
                Text("會議資料庫").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                Spacer()
                Text("\(desk.meetings.count)").font(.system(size: 11)).foregroundStyle(.secondary)
                Button(action: desk.refresh) { Image(systemName: "arrow.clockwise") }.buttonStyle(.plain).disabled(desk.busy)
            }
            ScrollView {
                LazyVStack(spacing: 7) {
                    ForEach(desk.meetings.filter { search.isEmpty || ($0.title + $0.date).localizedCaseInsensitiveContains(search) }) { meeting in
                        Button { desk.selected = meeting.id } label: {
                            VStack(alignment: .leading, spacing: 7) {
                                Text(meeting.title).font(.system(size: 13, weight: .semibold)).lineLimit(2)
                                Text(meeting.date).font(.system(size: 11)).foregroundStyle(.secondary)
                                Label(meeting.status, systemImage: meeting.reviewed ? "checkmark.circle.fill" : "circle.dotted")
                                    .font(.system(size: 10)).foregroundStyle(accent)
                            }.frame(maxWidth: .infinity, alignment: .leading).padding(12)
                                .background(desk.selected == meeting.id ? accent.opacity(0.13) : Color.clear)
                                .cornerRadius(9).contentShape(Rectangle())
                        }.buttonStyle(.plain).disabled(desk.busy)
                    }
                }
            }
            Divider()
            HStack {
                Label("音訊留在這台 Mac", systemImage: "internaldrive").font(.system(size: 10)).foregroundStyle(.secondary)
                Spacer()
                Button { desk.showSettings = true } label: { Image(systemName: "gearshape") }.buttonStyle(.plain).disabled(desk.busy)
            }
        }.padding(20).frame(width: 242).background(Color(nsColor: .controlBackgroundColor))
    }
    var welcome: some View {
        VStack(spacing: 20) {
            Spacer()
            Image(systemName: "waveform.badge.plus").font(.system(size: 58, weight: .ultraLight)).foregroundStyle(accent)
            Text("把錄音拖進來，從這裡開始。").font(.system(size: 24, weight: .medium))
            Text("語音備忘錄的 m4a 可直接匯入。\n不用先搬到 Transcripts，也不用記指令。")
                .multilineTextAlignment(.center).font(.system(size: 14)).foregroundStyle(.secondary).lineSpacing(6)
            Button("選擇音訊檔案", action: desk.chooseAudio).buttonStyle(.borderedProminent).tint(accent).controlSize(.large).disabled(desk.busy)
            HStack(spacing: 28) {
                Label("保留原始錄音", systemImage: "doc.on.doc")
                Label("中斷後可續跑", systemImage: "arrow.clockwise")
                Label("LLM 交接", systemImage: "arrow.up.doc")
            }.font(.system(size: 11)).foregroundStyle(.secondary).padding(.top, 18)
            Spacer()
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
    }
    func detail(_ meeting: Meeting) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(meeting.title).font(.system(size: 25, weight: .semibold)).textSelection(.enabled)
                        Button { desk.renameDraft = RenameDraft(folder: meeting.id, title: meeting.title) } label: {
                            Image(systemName: "pencil")
                        }.buttonStyle(.plain).help("修改會議名稱").disabled(desk.busy)
                    }
                    Text(meeting.date + "  ·  " + meeting.status).font(.system(size: 12)).foregroundStyle(.secondary)
                }
                Spacer()
                Button(action: desk.reveal) { Label("資料夾", systemImage: "folder") }
            }
            HStack(spacing: 0) {
                step("01", "保存音檔", true)
                step("02", "轉錄與預清洗", meeting.prepared)
                step("03", "LLM 清洗", meeting.cleaned)
                step("04", "內容確認", meeting.reviewed)
            }.padding(15).background(Color(nsColor: .controlBackgroundColor)).cornerRadius(12)
            if !meeting.error.isEmpty { Text(meeting.error).font(.system(size: 12)).foregroundStyle(.red).textSelection(.enabled) }
            HStack(spacing: 10) {
                if !meeting.prepared {
                    Button(meeting.raw ? "整理逐字稿" : "轉錄並整理", action: desk.run).buttonStyle(.borderedProminent).tint(accent)
                }
                Picker("提示詞", selection: $desk.profile) {
                    Text("選擇提示詞").tag("")
                    ForEach(desk.profiles.indices, id: \.self) { i in
                        Text(desk.profiles[i]["label"] as? String ?? "").tag(desk.profiles[i]["key"] as? String ?? "")
                    }
                }.frame(maxWidth: 260)
                Button("準備 LLM 交接") { desk.handoff() }.disabled(!meeting.prepared || desk.profile.isEmpty)
                Button(meeting.cleaned ? "重新檢查清洗稿" : "匯入清洗稿", action: desk.importCleaned).disabled(!meeting.prepared)
                Spacer(minLength: 0)
            }.disabled(desk.busy)
            if meeting.cleaned {
                HStack {
                    if !meeting.reviewed {
                        Button("已檢查，確認清洗稿") { desk.showReview = true }.buttonStyle(.borderedProminent).tint(accent)
                    } else {
                        Label("內容已由你確認", systemImage: "checkmark.seal.fill").foregroundStyle(accent)
                        Spacer()
                        Button("準備會議記錄交接") { desk.handoff(summary: true) }.disabled(desk.profile.isEmpty)
                    }
                }.font(.system(size: 12)).disabled(desk.busy)
            }
            HStack {
                Picker("預覽", selection: $desk.previewKind) {
                    Text("原始稿").tag("raw")
                    Text("預清洗稿").tag("prepared")
                    if meeting.corrected { Text("人工訂正").tag("corrected") }
                    Text("清洗稿").tag("cleaned")
                    Text("時間軸").tag("srt")
                }.pickerStyle(.segmented).frame(maxWidth: 440).disabled(desk.busy)
                Spacer()
                Button(action: desk.beginEdit) { Label("編輯文字", systemImage: "square.and.pencil") }
                    .disabled(desk.busy || !desk.previewEditable)
                Button {
                    if !desk.previewPath.isEmpty { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: desk.previewPath)]) }
                } label: { Image(systemName: "folder") }.disabled(desk.previewPath.isEmpty).help("在 Finder 顯示檔案")
            }
            if !desk.previewEditError.isEmpty {
                Text(desk.previewEditError).font(.caption).foregroundStyle(.orange)
            }
            if desk.previewKind == "srt" {
                Label("時間碼已鎖定，僅可訂正字幕文字", systemImage: "lock.fill").font(.caption).foregroundStyle(.secondary)
            }
            ScrollView {
                Text(desk.preview).font(.system(size: 14)).lineSpacing(7).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .topLeading).padding(20)
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color(nsColor: .textBackgroundColor)).cornerRadius(10)
            Text("清洗與摘要分開處理 · 原始逐字稿保留 · Notion 尚未自動發布")
                .font(.system(size: 10)).foregroundStyle(.secondary)
        }.padding(26)
    }
    func step(_ number: String, _ label: String, _ complete: Bool) -> some View {
        HStack(spacing: 8) {
            Text(complete ? "✓" : number).font(.system(size: 11, weight: .semibold))
                .frame(width: 27, height: 27).background(complete ? accent.opacity(0.16) : Color.secondary.opacity(0.08)).clipShape(Circle())
            Text(label).font(.system(size: 11, weight: .medium))
            Spacer(minLength: 4)
        }.foregroundStyle(complete ? accent : .secondary)
    }
}

struct TranscriptEditor: View {
    @ObservedObject var desk: Desk
    let draft: EditDraft
    @State private var text: String
    @State private var cues: [SubtitleCue]
    @State private var error = ""
    @State private var confirmDiscard = false
    init(desk: Desk, draft: EditDraft) {
        self.desk = desk
        self.draft = draft
        _text = State(initialValue: draft.text)
        _cues = State(initialValue: draft.cues)
    }
    var dirty: Bool { text != draft.text || cues.map(\.text) != draft.cues.map(\.text) }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 5) {
                    Text(draft.kind == "srt" ? "訂正字幕文字" : "人工訂正逐字稿").font(.title2.bold())
                    Text(draft.title).font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                if dirty { Text("尚未儲存").font(.caption).foregroundStyle(.orange) }
            }
            if draft.kind == "srt" {
                Label("時間、序號與段落順序已鎖定", systemImage: "lock.fill").font(.callout)
                Text("字幕訂正獨立保存，會放入 LLM 交接包；不會自動改寫 TXT 逐字稿。")
                    .font(.caption).foregroundStyle(.secondary)
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        ForEach($cues) { $cue in
                            VStack(alignment: .leading, spacing: 8) {
                                HStack {
                                    Text("#\(cue.number)")
                                    Text(cue.time).monospacedDigit()
                                    Spacer()
                                    Image(systemName: "lock.fill")
                                }.font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                                TextField("字幕文字", text: $cue.text, axis: .vertical)
                                    .lineLimit(2...8).textFieldStyle(.roundedBorder).disabled(desk.busy)
                            }.padding(12).background(Color(nsColor: .controlBackgroundColor)).cornerRadius(8)
                        }
                    }
                }
            } else {
                Text(draft.kind == "cleaned" ? "儲存後保留舊版清洗稿，並重新檢查內容。" : "儲存為「人工訂正」版本，後續清洗交接會使用這份內容。原始稿與預清洗稿仍可對照。")
                    .font(.callout).foregroundStyle(.secondary)
                TextEditor(text: $text).font(.system(size: 15)).lineSpacing(6)
                    .padding(8).background(Color(nsColor: .textBackgroundColor)).cornerRadius(8).disabled(desk.busy)
            }
            if !error.isEmpty {
                Text(error).font(.callout).foregroundStyle(.red).textSelection(.enabled)
                Button("複製目前修改") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(draft.kind == "srt" ? cues.map { "\($0.number)\n\($0.time)\n\($0.text)" }.joined(separator: "\n\n") : text, forType: .string)
                }.disabled(desk.busy)
            }
            HStack {
                Text(draft.kind == "srt" ? "\(cues.count) 段字幕" : "\(text.count) 字").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("取消") {
                    if dirty { confirmDiscard = true } else { desk.editor = nil }
                }.keyboardShortcut(.cancelAction).disabled(desk.busy)
                Button(desk.busy ? "儲存中…" : "儲存訂正") {
                    error = ""
                    desk.saveEdit(draft, text: text, cues: cues) { error = $0 }
                }.buttonStyle(.borderedProminent).keyboardShortcut("s", modifiers: .command)
                    .disabled(desk.busy || !dirty)
            }
        }.padding(24).frame(width: 800, height: 620)
        .background(Color(nsColor: .windowBackgroundColor))
        .interactiveDismissDisabled(true)
        .onChange(of: dirty) { desk.hasUnsavedEdits = dirty }
        .onDisappear { desk.hasUnsavedEdits = false }
        .alert("放棄尚未儲存的修改？", isPresented: $confirmDiscard) {
            Button("繼續編輯", role: .cancel) {}
            Button("放棄修改", role: .destructive) { desk.hasUnsavedEdits = false; desk.editor = nil }
        } message: { Text("這次編輯的內容尚未儲存，已儲存的版本不受影響。") }
    }
}

struct RenameMeetingView: View {
    @ObservedObject var desk: Desk
    let draft: RenameDraft
    @State private var title: String
    @State private var error = ""
    @State private var confirmDiscard = false
    init(desk: Desk, draft: RenameDraft) {
        self.desk = desk
        self.draft = draft
        _title = State(initialValue: draft.title)
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("修改會議名稱").font(.title2.bold())
            TextField("會議名稱", text: $title).textFieldStyle(.roundedBorder).disabled(desk.busy)
            Text("更新清單與後續交接顯示的名稱，音檔及資料夾位置保持不變。")
                .font(.callout).foregroundStyle(.secondary)
            if !error.isEmpty { Text(error).foregroundStyle(.red).textSelection(.enabled) }
            HStack {
                Button("取消") {
                    if title != draft.title { confirmDiscard = true } else { desk.renameDraft = nil }
                }.keyboardShortcut(.cancelAction).disabled(desk.busy)
                Spacer()
                Button("儲存名稱") {
                    desk.call(["action": "rename", "folder": draft.folder, "title": title, "expected_title": draft.title], failed: { error = $0 }) { _ in
                        desk.hasUnsavedEdits = false
                        desk.renameDraft = nil
                        desk.notice = "會議名稱已更新。"
                        desk.refresh()
                    }
                }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                    .disabled(desk.busy || title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || title == draft.title)
            }
        }.padding(28).frame(width: 480)
        .background(Color(nsColor: .windowBackgroundColor))
        .interactiveDismissDisabled(true)
        .onChange(of: title) { desk.hasUnsavedEdits = title != draft.title }
        .onDisappear { desk.hasUnsavedEdits = false }
        .alert("放棄名稱修改？", isPresented: $confirmDiscard) {
            Button("繼續編輯", role: .cancel) {}
            Button("放棄修改", role: .destructive) { desk.hasUnsavedEdits = false; desk.renameDraft = nil }
        }
    }
}

struct ImportView: View {
    @ObservedObject var desk: Desk
    @State var draft: [String: String]
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Label("加入一場會議", systemImage: "waveform").font(.title2.bold())
            Text(URL(fileURLWithPath: draft["path"] ?? "").lastPathComponent).foregroundStyle(.secondary).lineLimit(2)
            VStack(alignment: .leading, spacing: 8) {
                Text("會議名稱").font(.caption)
                TextField("會議名稱", text: Binding(get: { draft["title"] ?? "" }, set: { draft["title"] = $0 }))
                Text("錄音時間（台北時間）").font(.caption).padding(.top, 8)
                TextField("YYYY-MM-DD HH:MM", text: Binding(get: { draft["meeting_time"] ?? "" }, set: { draft["meeting_time"] = $0 }))
                Text("偵測來源：" + (draft["time_source"] ?? "") + "。可在匯入前修正。").font(.caption).foregroundStyle(.secondary)
            }.textFieldStyle(.roundedBorder)
            Text("工具會保存音檔副本並建立會議資料夾，原檔仍留在原處。")
                .font(.callout).foregroundStyle(.secondary)
            HStack {
                Button("取消") { desk.importDraft = nil }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("匯入會議") { desk.addAudio(draft) }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
            }
        }.padding(30).frame(width: 480)
    }
}

struct SettingsView: View {
    @ObservedObject var desk: Desk
    @Environment(\.dismiss) var dismiss
    @State private var output = ""
    @State private var whisper = ""
    @State private var model = "medium"
    @State private var threads = 8
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("本機設定").font(.title2.bold())
            Text("中文辨識，保留英文原詞。繁體校正在後續清洗完成。").font(.callout).foregroundStyle(.secondary)
            Form {
                TextField("會議資料夾", text: $output)
                TextField("whisper.cpp 資料夾", text: $whisper)
                Picker("多語言模型", selection: $model) {
                    ForEach(["tiny", "base", "small", "medium", "large-v3"], id: \.self) { Text($0).tag($0) }
                }
                Stepper("執行緒：\(threads)", value: $threads, in: 1...256)
            }
            Divider()
            ForEach(desk.checks.indices, id: \.self) { i in
                Label(desk.checks[i]["name"] as? String ?? "", systemImage: desk.checks[i]["ok"] as? Bool == true ? "checkmark.circle.fill" : "exclamationmark.triangle")
                    .foregroundStyle(desk.checks[i]["ok"] as? Bool == true ? .green : .orange)
            }
            Text("模型需已存在於 whisper.cpp/models。設定只套用新轉錄，不會重做既有逐字稿。變更會議資料夾會切換清單，不會搬移舊資料。")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("取消") { dismiss() }
                Spacer()
                Button("儲存") {
                    desk.call(["action": "settings", "settings": ["output_root": output, "whisper_root": whisper, "model": model, "threads": threads]]) { _ in
                        dismiss()
                        desk.load()
                    }
                }.buttonStyle(.borderedProminent).disabled(desk.busy)
            }
        }.padding(30).frame(width: 540)
        .onAppear {
            output = desk.settings["output_root"] as? String ?? ""
            whisper = desk.settings["whisper_root"] as? String ?? ""
            model = desk.settings["model"] as? String ?? "medium"
            threads = desk.settings["threads"] as? Int ?? 8
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    static var desk: Desk?
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if let desk = Self.desk, desk.hasUnsavedEdits {
            let alert = NSAlert()
            alert.messageText = "有尚未儲存的修改"
            alert.informativeText = "請回到編輯視窗儲存，或按取消放棄修改，再結束 App。"
            alert.runModal()
            return .terminateCancel
        }
        if let desk = Self.desk, desk.busy {
            let alert = NSAlert()
            alert.messageText = "目前仍有操作進行中"
            alert.informativeText = "請先取消轉錄並等候停止，或等待目前操作完成，再關閉工具。"
            alert.runModal()
            return .terminateCancel
        }
        return .terminateNow
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { sender.windows.first?.makeKeyAndOrderFront(nil) }
        return true
    }
}

#if !DESKTOP_UI_TEST
@main
struct MeetingDeskApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject var desk = Desk()
    var body: some Scene {
        WindowGroup("Meeting Desk") {
            ContentView(desk: desk)
                .onAppear {
                    AppDelegate.desk = desk
                    if let iconURL = Bundle.main.url(forResource: "MeetingDesk", withExtension: "icns"),
                       let icon = NSImage(contentsOf: iconURL) {
                        NSApp.applicationIconImage = icon
                    }
                    NSApp.setActivationPolicy(.regular)
                    NSApp.activate(ignoringOtherApps: true)
                    // Developer visual QA: captures only this app's own content view.
                    if let flag = CommandLine.arguments.firstIndex(of: "--snapshot"), CommandLine.arguments.count > flag + 1 {
                        let destination = CommandLine.arguments[flag + 1]
                        DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                            guard let view = NSApp.windows.first?.contentView,
                                  let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
                            view.cacheDisplay(in: view.bounds, to: bitmap)
                            if let data = bitmap.representation(using: .png, properties: [:]) { try? data.write(to: URL(fileURLWithPath: destination)) }
                        }
                    }
                }
                .onOpenURL { desk.inspect($0) }
        }.defaultSize(width: 1180, height: 790)
        .commands {
            CommandGroup(replacing: .newItem) { Button("匯入音檔…", action: desk.chooseAudio).keyboardShortcut("o").disabled(desk.busy) }
            CommandGroup(replacing: .appSettings) { Button("設定…") { desk.showSettings = true }.keyboardShortcut(",").disabled(desk.busy) }
        }
    }
}
#endif
