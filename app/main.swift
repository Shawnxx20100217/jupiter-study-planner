import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, NSToolbarDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var paths: [String: String] = [:]
    private var statusItem: NSToolbarItem!
    private var refreshTimer: Timer?
    private var syncInFlight = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Use the light Aqua chrome for a calm, compact native frame around
        // the dashboard. The dashboard itself owns its colors and layout.
        NSApp.appearance = NSAppearance(named: .aqua)
        // Keep one visible app window. This also prevents an older copy from
        // competing with the current bundle when the app is rebuilt.
        let instances = NSRunningApplication.runningApplications(withBundleIdentifier: "local.jupiter.study-planner")
        if instances.count > 1 {
            instances.first(where: { $0 != NSRunningApplication.current })?.activate(options: [.activateAllWindows])
            NSApp.terminate(nil)
            return
        }
        loadPaths()
        let configuration = WKWebViewConfiguration()
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.setValue(false, forKey: "drawsBackground")
        webView.navigationDelegate = self

        window = NSWindow(contentRect: NSMakeRect(0, 0, 1180, 720),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: .aqua)
        window.title = "Jupiter 作业管家"
        window.toolbarStyle = .unifiedCompact
        window.titleVisibility = .visible
        window.titlebarAppearsTransparent = false
        window.backgroundColor = .windowBackgroundColor
        window.minSize = NSSize(width: 760, height: 500)
        window.center()
        window.contentView = webView
        window.toolbar = makeToolbar()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        loadDashboard()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            self?.syncIfStale()
        }
        syncIfStale()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    private func loadPaths() {
        if let url = Bundle.main.url(forResource: "paths", withExtension: "json"),
           let data = try? Data(contentsOf: url),
           let decoded = try? JSONSerialization.jsonObject(with: data) as? [String: String] {
            paths = decoded
        }

        // Keep account data and machine-specific paths outside the app bundle.
        // A shared copy uses the conventional ~/JupiterStudyPlanner layout;
        // the checkout fallback keeps this development build immediately useful.
        let home = FileManager.default.homeDirectoryForCurrentUser
        let bundleParent = Bundle.main.bundleURL.deletingLastPathComponent()
        let projectRoot = bundleParent.deletingLastPathComponent()
        let pluginRoot = bundleParent.appendingPathComponent("jupiter-study-planner")
        let standardReports = home.appendingPathComponent("JupiterStudyPlanner/reports", isDirectory: true)
        let localReports = bundleParent
        let reports = directory([standardReports, localReports])
        paths["reports"] = paths["reports"] ?? reports.path
        paths["dashboard"] = paths["dashboard"] ?? firstFile([
            reports.appendingPathComponent("study-plan.html"),
            reports.appendingPathComponent("my-study-plan.html"),
            localReports.appendingPathComponent("my-study-plan.html")
        ])?.path ?? reports.appendingPathComponent("study-plan.html").path
        paths["instance"] = paths["instance"] ?? firstFile([
            home.appendingPathComponent("JupiterStudyPlanner/state/local-instance.json"),
            projectRoot.appendingPathComponent("work/jupiter-state/local-instance.json")
        ])?.path ?? home.appendingPathComponent("JupiterStudyPlanner/state/local-instance.json").path
        paths["state"] = paths["state"] ?? URL(fileURLWithPath: paths["instance"]!).deletingLastPathComponent().path
        paths["scripts"] = paths["scripts"] ?? pluginRoot.appendingPathComponent("scripts").path
        paths["python"] = paths["python"] ?? firstFile([
            home.appendingPathComponent(".jupiter-runtime/bin/python"),
            projectRoot.appendingPathComponent("work/jupiter-runtime/bin/python"),
            URL(fileURLWithPath: "/usr/bin/python3")
        ])?.path ?? "/usr/bin/python3"
    }

    private func directory(_ candidates: [URL]) -> URL {
        candidates.first(where: { FileManager.default.fileExists(atPath: $0.path) }) ?? candidates[0]
    }

    private func firstFile(_ candidates: [URL]) -> URL? {
        candidates.first(where: { FileManager.default.fileExists(atPath: $0.path) })
    }

    private func loadDashboard() {
        guard let raw = paths["dashboard"] else { return }
        var url = URL(fileURLWithPath: raw)
        // A background sync redraws the local file. Preserve the selected view
        // so the user does not get thrown back to the task list.
        if let fragment = webView?.url?.fragment,
           var components = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            components.fragment = fragment
            url = components.url ?? url
        }
        let directory = url.deletingLastPathComponent()
        webView.loadFileURL(url, allowingReadAccessTo: directory)
    }

    private func syncIfStale() {
        guard !syncInFlight, let state = paths["state"] else { return }
        let statusURL = URL(fileURLWithPath: state).appendingPathComponent("run-status.json")
        var stale = true
        if let data = try? Data(contentsOf: statusURL),
           let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let text = value["last_attempt"] as? String {
            let formatter = ISO8601DateFormatter()
            if let date = formatter.date(from: text) { stale = Date().timeIntervalSince(date) >= 900 }
        }
        if stale { runPython(arguments: ["runtime", "run"], success: "已自动检查 Jupiter。") }
    }

    private func makeToolbar() -> NSToolbar {
        let toolbar = NSToolbar(identifier: NSToolbar.Identifier("JupiterToolbar"))
        toolbar.delegate = self
        toolbar.displayMode = .iconAndLabel
        toolbar.sizeMode = .small
        toolbar.allowsUserCustomization = false
        return toolbar
    }

    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.sync, .plan, .reports, .flexibleSpace, .status]
    }
    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.sync, .plan, .flexibleSpace, .status, .reports]
    }
    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier identifier: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        if identifier == .flexibleSpace { return NSToolbarItem(itemIdentifier: identifier) }
        let item = NSToolbarItem(itemIdentifier: identifier)
        if identifier == .sync {
            item.label = "同步 Jupiter"; item.paletteLabel = item.label; item.toolTip = "读取最新作业并更新看板"; item.image = NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: nil); item.target = self; item.action = #selector(sync)
        } else if identifier == .plan {
            item.label = "现在有几分钟？"; item.paletteLabel = item.label; item.toolTip = "按你现在可用的时间生成学习段"; item.image = NSImage(systemSymbolName: "clock", accessibilityDescription: nil); item.target = self; item.action = #selector(plan)
        } else if identifier == .reports {
            item.label = "打开文件夹"; item.paletteLabel = item.label; item.toolTip = "打开报告和日历文件"; item.image = NSImage(systemSymbolName: "folder", accessibilityDescription: nil); item.target = self; item.action = #selector(openReports)
        } else if identifier == .status {
            item.label = "本地模式"; item.paletteLabel = item.label; item.toolTip = "读取和规划在本机完成，不调用模型"; item.image = NSImage(systemSymbolName: "lock.shield", accessibilityDescription: nil)
            statusItem = item
        }
        return item
    }

    @objc private func sync() {
        runPython(arguments: ["runtime", "run"], success: "同步完成，正在刷新看板。")
    }

    @objc private func plan() {
        let alert = NSAlert()
        alert.messageText = "你现在有多少分钟？"
        alert.informativeText = "只规划这一段，不会推断你未来每天的时间。"
        alert.addButton(withTitle: "生成安排")
        alert.addButton(withTitle: "取消")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 240, height: 24))
        field.placeholderString = "例如 20"
        field.stringValue = "20"
        alert.accessoryView = field
        guard alert.runModal() == .alertFirstButtonReturn, let minutes = Int(field.stringValue.trimmingCharacters(in: .whitespaces)), minutes > 0, minutes <= 720 else { return }
        runPython(arguments: ["planner", "plan", String(minutes)], success: "已生成这一段学习安排。")
    }

    @objc private func openReports() {
        guard let raw = paths["reports"] else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: raw))
    }

    private func runPython(arguments: [String], success: String) {
        guard let python = paths["python"], let scripts = paths["scripts"], let instance = paths["instance"], let state = paths["state"], let reports = paths["reports"] else { showError("本机路径配置不完整。"); return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        if arguments.first == "runtime" {
            process.arguments = [scripts + "/jupiter_runtime.py", "run", "--instance", instance]
            syncInFlight = true
        } else if arguments.first == "notice-read", let id = arguments.dropFirst().first {
            process.arguments = [scripts + "/jupiter_runtime.py", "mark-notice-read", "--instance", instance, "--notice-id", id]
        } else if arguments.first == "task-complete" || arguments.first == "task-reopen", let id = arguments.dropFirst().first {
            var command = [scripts + "/jupiter_runtime.py", "mark-task-complete", "--instance", instance, "--task-id", id]
            if arguments.first == "task-reopen" { command.append("--open") }
            process.arguments = command
        } else {
            let minutes = arguments.last ?? "20"
            process.arguments = [scripts + "/planner.py", "plan", "--state", state, "--minutes", minutes, "--output", reports + "/session.md"]
        }
        process.currentDirectoryURL = URL(fileURLWithPath: scripts).deletingLastPathComponent()
        let pipe = Pipe(); process.standardOutput = pipe; process.standardError = pipe
        setStatus("正在处理…")
        process.terminationHandler = { [weak self] task in
            DispatchQueue.main.async {
                guard let self else { return }
                if arguments.first == "runtime" { self.syncInFlight = false }
                self.setStatus(task.terminationStatus == 0 ? success : "处理失败；请看板顶部的状态说明。")
                if task.terminationStatus == 0 { self.loadDashboard() }
                else { self.showError("本次操作没有完成。看板会保留上一次成功的数据。") }
            }
        }
        do { try process.run() } catch { showError("无法启动本机运行器：\(error.localizedDescription)") }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url, url.scheme == "jupiter" else {
            decisionHandler(.allow); return
        }
        let action = url.host ?? url.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let id = query.first(where: { $0.name == "id" })?.value
        if let id {
            if action == "notice-read" { runPython(arguments: ["notice-read", id], success: "通知已标记为已读。") }
            if action == "task-complete" { runPython(arguments: ["task-complete", id], success: "已记录本地完成。") }
            if action == "task-reopen" { runPython(arguments: ["task-reopen", id], success: "已重新打开本地任务。") }
        }
        decisionHandler(.cancel)
    }

    private func setStatus(_ text: String) { statusItem?.label = text }
    private func showError(_ text: String) { let alert = NSAlert(); alert.messageText = "Jupiter 作业管家"; alert.informativeText = text; alert.runModal() }
}

extension NSToolbarItem.Identifier {
    static let sync = NSToolbarItem.Identifier("sync")
    static let plan = NSToolbarItem.Identifier("plan")
    static let reports = NSToolbarItem.Identifier("reports")
    static let status = NSToolbarItem.Identifier("status")
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
