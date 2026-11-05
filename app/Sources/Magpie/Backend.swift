import Foundation

@MainActor
final class BackendLauncher {
    private var process: Process?
    private var logHandle: FileHandle?
    private(set) var lastError: String?

    var repoPath: String { RepoLocation.path }

    var executablePath: String {
        (repoPath as NSString).appendingPathComponent(".venv/bin/magpie")
    }

    var isRunning: Bool {
        guard let process else { return false }
        return process.isRunning
    }

    func start() {
        guard process == nil else { return }
        lastError = nil

        guard FileManager.default.isExecutableFile(atPath: executablePath) else {
            lastError =
                "Magpie backend was not found at \(executablePath). "
                + "Install it with: uv pip install -e ."
            return
        }

        let logURL = BackendLauncher.logURL()
        try? FileManager.default.createDirectory(
            at: logURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let handle = try? FileHandle(forWritingTo: logURL)
        handle?.seekToEndOfFile()

        let process = Process()
        process.executableURL = URL(fileURLWithPath: executablePath)
        process.arguments = ["serve"]
        process.currentDirectoryURL = URL(fileURLWithPath: repoPath, isDirectory: true)
        var environment = ProcessInfo.processInfo.environment
        environment["MAGPIE_REPO"] = repoPath
        process.environment = environment
        if let handle {
            process.standardOutput = handle
            process.standardError = handle
        }

        process.terminationHandler = { [weak self] finished in
            let code = finished.terminationStatus
            Task { @MainActor in
                guard let self else { return }
                if self.process === finished {
                    self.process = nil
                }
                if code != 0 {
                    self.lastError = "Magpie backend exited with status \(code)."
                }
            }
        }

        do {
            try process.run()
            self.process = process
            self.logHandle = handle
        } catch {
            lastError = "Could not start Magpie: \(error.localizedDescription)"
        }
    }

    func stop() {
        guard let process else { return }
        if process.isRunning {
            process.terminate()
            let pid = process.processIdentifier
            DispatchQueue.global(qos: .background).asyncAfter(deadline: .now() + 2.0) {
                if process.isRunning {
                    kill(pid, SIGKILL)
                }
            }
        }
        self.process = nil
        try? logHandle?.close()
        logHandle = nil
    }

    static func logURL() -> URL {
        let base = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library")
        return base
            .appendingPathComponent("Logs", isDirectory: true)
            .appendingPathComponent("Magpie", isDirectory: true)
            .appendingPathComponent("backend.log")
    }
}
