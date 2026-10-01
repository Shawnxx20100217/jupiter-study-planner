import Cocoa

private struct SyncState {
    var enabled = false
    var launchAtLogin = false
    var jobsLoaded = false
    var stopping = false
    var jupiterLastSuccess: String?
    var ticktickLastSuccess: String?
    var mode = "plan"
    var modeLabel = "计划同步"
    var lastError: String?
    var available = false

    init(_ value: [String: Any] = [:]) {
        available = value["ok"] as? Bool == true
        enabled = value["enabled"] as? Bool == true
        launchAtLogin = value["launch_at_login"] as? Bool == true
        jobsLoaded = value["jobs_loaded"] as? Bool == true
        stopping = value["stopping"] as? Bool == true
        jupiterLastSuccess = value["jupiter_last_success"] as? String
        ticktickLastSuccess = value["ticktick_last_success"] as? String
        mode = value["mode"] as? String ?? "plan"
        modeLabel = value["mode_label"] as? String ?? (mode == "source_mirror" ? "原始同步" : "计划同步")
        if let error = value["last_error"] as? String, !error.isEmpty { lastError = error }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var window: NSWindow!
    private var paths: [String: String] = [:]
    private var state = SyncState()
    private var timer: Timer?
    private var busy = false
    private var refreshing = false
    private var syncSwitch: NSSwitch!
    private var loginSwitch: NSSwitch!
    private var syncButton: NSButton!
    private var statusDot: NSImageView!
    private var statusLabel: NSTextField!
    private var lastSyncLabel: NSTextField!
    private var detailLabel: NSTextField!

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.appearance = NSAppearance(named: .aqua)
        let instances = NSRunningApplication.runningApplications(withBundleIdentifier: "local.jupiter.study-planner")
        if let existing = instances.first(where: { $0 != NSRunningApplication.current }) {
            existing.activate(options: [.activateAllWindows])
            NSApp.terminate(nil)
            return
        }
        loadPaths()
        makeMenu()
        buildWindow()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        refreshStatus()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.refreshStatus() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        refreshStatus()
        return true
    }

    private func loadPaths() {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let external = home.appendingPathComponent("Library/Application Support/Jupiter Study Planner/paths.json")
        let personal = home.appendingPathComponent("JupiterStudyPlanner/paths.json")
        let candidates = [external, personal, Bundle.main.url(forResource: "paths", withExtension: "json")].compactMap { $0 }
        for url in candidates {
            if let data = try? Data(contentsOf: url),
               let value = try? JSONSerialization.jsonObject(with: data) as? [String: String] {
                paths = value
                break
            }
        }
        let bundleParent = Bundle.main.bundleURL.deletingLastPathComponent()
        paths["instance"] = paths["instance"] ?? firstFile([
            home.appendingPathComponent("JupiterStudyPlanner/state/local-instance.json")
        ])?.path ?? home.appendingPathComponent("JupiterStudyPlanner/state/local-instance.json").path
        paths["state"] = paths["state"] ?? URL(fileURLWithPath: paths["instance"]!).deletingLastPathComponent().path
        paths["scripts"] = paths["scripts"] ?? bundleParent.appendingPathComponent("jupiter-study-planner/scripts").path
        paths["python"] = paths["python"] ?? firstFile([
            home.appendingPathComponent(".jupiter-runtime/bin/python"),
            URL(fileURLWithPath: "/usr/bin/python3")
        ])?.path ?? "/usr/bin/python3"
    }

    private func firstFile(_ candidates: [URL]) -> URL? {
        candidates.first(where: { FileManager.default.fileExists(atPath: $0.path) })
    }

    private func makeMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "显示同步控制", action: #selector(showWindow), keyEquivalent: "0").target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 Jupiter 作业管家", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)
        NSApp.mainMenu = main
    }

    @objc private func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func label(_ text: String, size: CGFloat, weight: NSFont.Weight = .regular, color: NSColor = .labelColor) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = NSFont.systemFont(ofSize: size, weight: weight)
        field.textColor = color
        field.translatesAutoresizingMaskIntoConstraints = false
        return field
    }

    private func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 500, height: 440),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "Jupiter 作业管家"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.backgroundColor = NSColor(calibratedWhite: 0.975, alpha: 1)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()
        let view = NSView()
        window.contentView = view
        let header = label("Jupiter  →  TickTick", size: 25, weight: .semibold)
        let subtitle = label("让作业自动出现在你的清单里", size: 13, color: .secondaryLabelColor)
        statusDot = NSImageView(image: NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)!)
        statusDot.contentTintColor = .tertiaryLabelColor
        statusDot.translatesAutoresizingMaskIntoConstraints = false
        statusLabel = label("正在读取状态…", size: 13, weight: .medium)
        lastSyncLabel = label("", size: 12, color: .secondaryLabelColor)
        let statusRow = NSStackView(views: [statusDot, statusLabel])
        statusRow.orientation = .horizontal
        statusRow.spacing = 7
        statusRow.alignment = .centerY
        statusRow.translatesAutoresizingMaskIntoConstraints = false

        let card = NSView()
        card.wantsLayer = true
        card.layer?.backgroundColor = NSColor.white.cgColor
        card.layer?.cornerRadius = 12
        card.layer?.borderWidth = 1
        card.layer?.borderColor = NSColor(calibratedWhite: 0.9, alpha: 1).cgColor
        card.translatesAutoresizingMaskIntoConstraints = false
        syncSwitch = NSSwitch()
        syncSwitch.target = self
        syncSwitch.action = #selector(settingsChanged)
        loginSwitch = NSSwitch()
        loginSwitch.target = self
        loginSwitch.action = #selector(settingsChanged)
        let syncRow = settingRow("后台自动同步", detail: "关闭后，Jupiter 和 TickTick 都停止同步", toggle: syncSwitch)
        let loginRow = settingRow("登录 Mac 时自动启动", detail: "自动在后台运行，不弹出窗口", toggle: loginSwitch)
        let divider = NSBox()
        divider.boxType = .separator
        divider.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(syncRow)
        card.addSubview(loginRow)
        card.addSubview(divider)
        NSLayoutConstraint.activate([
            syncRow.topAnchor.constraint(equalTo: card.topAnchor, constant: 15), syncRow.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 18), syncRow.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -18),
            divider.topAnchor.constraint(equalTo: syncRow.bottomAnchor, constant: 13), divider.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 18), divider.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -18),
            loginRow.topAnchor.constraint(equalTo: divider.bottomAnchor, constant: 13), loginRow.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 18), loginRow.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -18), loginRow.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -15)
        ])
        syncButton = NSButton(title: "立即同步", target: self, action: #selector(syncNow))
        syncButton.bezelStyle = .rounded
        syncButton.controlSize = .large
        syncButton.keyEquivalent = "\r"
        let openButton = NSButton(title: "打开 TickTick ↗", target: self, action: #selector(openTickTick))
        openButton.bezelStyle = .rounded
        openButton.controlSize = .large
        let buttons = NSStackView(views: [syncButton, openButton])
        buttons.orientation = .horizontal
        buttons.spacing = 10
        buttons.distribution = .fillEqually
        buttons.translatesAutoresizingMaskIntoConstraints = false
        detailLabel = label("关闭此窗口不影响后台同步", size: 11, color: .secondaryLabelColor)
        detailLabel.lineBreakMode = .byWordWrapping
        detailLabel.maximumNumberOfLines = 3
        view.addSubview(header)
        view.addSubview(subtitle)
        view.addSubview(statusRow)
        view.addSubview(lastSyncLabel)
        view.addSubview(card)
        view.addSubview(buttons)
        view.addSubview(detailLabel)
        NSLayoutConstraint.activate([
            header.topAnchor.constraint(equalTo: view.topAnchor, constant: 20), header.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 30),
            subtitle.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 6), subtitle.leadingAnchor.constraint(equalTo: header.leadingAnchor),
            statusRow.topAnchor.constraint(equalTo: subtitle.bottomAnchor, constant: 22), statusRow.leadingAnchor.constraint(equalTo: header.leadingAnchor), statusDot.widthAnchor.constraint(equalToConstant: 8), statusDot.heightAnchor.constraint(equalToConstant: 8),
            lastSyncLabel.topAnchor.constraint(equalTo: statusRow.bottomAnchor, constant: 6), lastSyncLabel.leadingAnchor.constraint(equalTo: header.leadingAnchor),
            card.topAnchor.constraint(equalTo: lastSyncLabel.bottomAnchor, constant: 20), card.leadingAnchor.constraint(equalTo: header.leadingAnchor), card.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -30),
            buttons.topAnchor.constraint(equalTo: card.bottomAnchor, constant: 22), buttons.leadingAnchor.constraint(equalTo: header.leadingAnchor), buttons.trailingAnchor.constraint(equalTo: card.trailingAnchor),
            detailLabel.topAnchor.constraint(equalTo: buttons.bottomAnchor, constant: 13), detailLabel.leadingAnchor.constraint(equalTo: header.leadingAnchor), detailLabel.trailingAnchor.constraint(equalTo: card.trailingAnchor)
        ])
        render()
    }

    private func settingRow(_ title: String, detail: String, toggle: NSSwitch) -> NSView {
        let heading = label(title, size: 13, weight: .medium)
        let caption = label(detail, size: 11, color: .secondaryLabelColor)
        let words = NSStackView(views: [heading, caption])
        words.orientation = .vertical
        words.alignment = .leading
        words.spacing = 4
        let row = NSStackView(views: [words, toggle])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.distribution = .fill
        row.spacing = 12
        row.translatesAutoresizingMaskIntoConstraints = false
        words.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return row
    }

    private func render() {
        syncSwitch?.state = state.enabled ? .on : .off
        loginSwitch?.state = state.launchAtLogin ? .on : .off
        syncSwitch?.isEnabled = !busy && state.available
        loginSwitch?.isEnabled = !busy && state.available
        syncButton?.isEnabled = !busy && state.available && state.enabled
        if !busy {
            let bothSidesHaveSucceeded = state.jupiterLastSuccess != nil && state.ticktickLastSuccess != nil
            let healthy = state.enabled && state.jobsLoaded && bothSidesHaveSucceeded && state.lastError == nil
            statusLabel?.stringValue = !state.available ? "暂时无法读取同步状态" : state.stopping ? "正在停止 · 本轮结束后暂停" : state.enabled ? (healthy ? "后台同步已开启 · \(state.modeLabel)" : "同步已开启，等待两侧成功") : "同步已暂停"
            statusDot?.contentTintColor = !state.available ? .systemOrange : state.stopping ? .systemOrange : healthy ? .systemGreen : state.enabled ? .systemOrange : .tertiaryLabelColor
            detailLabel?.stringValue = state.lastError ?? (state.mode == "source_mirror" ? "原始同步只镜像 Jupiter 到 TickTick，不会覆盖学习计划区" : "关闭此窗口不影响后台同步")
            detailLabel?.textColor = state.lastError == nil ? .secondaryLabelColor : .systemOrange
        }
        let jupiter = state.jupiterLastSuccess.map { displayDate($0) } ?? "未成功"
        let ticktick = state.ticktickLastSuccess.map { displayDate($0) } ?? "未成功"
        lastSyncLabel?.stringValue = "Jupiter 读取  ·  \(jupiter)\nTickTick 写入  ·  \(ticktick)"
    }

    private func displayDate(_ text: String) -> String {
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = iso.date(from: text) ?? ISO8601DateFormatter().date(from: text)
        guard let date else { return text }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = Calendar.current.isDateInToday(date) ? "今天 HH:mm" : "M月d日 HH:mm"
        return formatter.string(from: date)
    }

    private func refreshStatus() {
        guard !busy, !refreshing else { return }
        refreshing = true
        runCommand(script: "sync_control.py", arguments: ["status"], timeout: 12) { [weak self] result in
            guard let self else { return }
            self.refreshing = false
            switch result {
            case .success(let value): self.state = SyncState(value)
            case .failure(let error): self.state.available = false; self.state.lastError = error.localizedDescription
            }
            self.render()
        }
    }

    @objc private func settingsChanged() {
        guard !busy else { return }
        let enabled = syncSwitch.state == .on
        let login = loginSwitch.state == .on
        busy = true
        render()
        statusLabel.stringValue = "正在保存设置…"
        runCommand(script: "sync_control.py", arguments: ["configure", "--python", paths["python"] ?? "/usr/bin/python3", "--enabled", enabled ? "on" : "off", "--login", login ? "on" : "off"], timeout: 30) { [weak self] result in
            guard let self else { return }
            self.busy = false
            switch result {
            case .success(let value): self.state = SyncState(value)
            case .failure(let error): self.state.lastError = error.localizedDescription
            }
            self.render()
            self.refreshStatus()
        }
    }

    @objc private func syncNow() {
        guard !busy, state.enabled else { return }
        busy = true
        render()
        statusLabel.stringValue = "正在同步 Jupiter → TickTick…"
        detailLabel.stringValue = "同步期间可以继续使用 TickTick"
        runCommand(script: "jupiter_runtime.py", arguments: ["run"], timeout: 240) { [weak self] result in
            guard let self else { return }
            self.busy = false
            if case .failure(let error) = result { self.state.lastError = error.localizedDescription }
            self.render()
            self.refreshStatus()
        }
    }

    @objc private func openTickTick() {
        if let url = URL(string: "https://ticktick.com/webapp/") { NSWorkspace.shared.open(url) }
    }

    private func runCommand(script: String, arguments: [String], timeout: TimeInterval,
                            completion: @escaping (Result<[String: Any], Error>) -> Void) {
        guard let python = paths["python"], let scripts = paths["scripts"], let instance = paths["instance"] else {
            completion(.failure(NSError(domain: "Jupiter", code: 1, userInfo: [NSLocalizedDescriptionKey: "本机配置不完整"])))
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: python)
            process.arguments = [scripts + "/" + script] + arguments + ["--instance", instance]
            process.currentDirectoryURL = URL(fileURLWithPath: scripts).deletingLastPathComponent()
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = pipe
            let completed = DispatchSemaphore(value: 0)
            process.terminationHandler = { _ in completed.signal() }
            do {
                try process.run()
                // Drain stdout while the child runs so a verbose sync cannot
                // block on a full pipe; keep UI operations on the main queue.
                let reader = DispatchGroup()
                reader.enter()
                var output = Data()
                DispatchQueue.global(qos: .utility).async {
                    output = pipe.fileHandleForReading.readDataToEndOfFile()
                    reader.leave()
                }
                if completed.wait(timeout: .now() + timeout) == .timedOut {
                    process.terminate()
                    _ = completed.wait(timeout: .now() + 5)
                    throw NSError(domain: "Jupiter", code: 2, userInfo: [NSLocalizedDescriptionKey: "本次操作超时，请稍后重试"])
                }
                reader.wait()
                let object = (try? JSONSerialization.jsonObject(with: output)) as? [String: Any]
                if process.terminationStatus != 0 || object?["ok"] as? Bool == false {
                    let message = object?["message"] as? String ?? "本次操作未完成，请稍后重试"
                    throw NSError(domain: "Jupiter", code: Int(process.terminationStatus), userInfo: [NSLocalizedDescriptionKey: message])
                }
                guard let value = object else {
                    throw NSError(domain: "Jupiter", code: 3, userInfo: [NSLocalizedDescriptionKey: "未能读取程序返回的状态"])
                }
                DispatchQueue.main.async { completion(.success(value)) }
            } catch {
                DispatchQueue.main.async { completion(.failure(error)) }
            }
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
